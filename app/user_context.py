"""Подписанный контекст пользователя вместо голых заголовков X-User-Id / X-User-Role.

Раньше nginx после проверки токена клиента просто дописывал запросу `X-User-Id` и
`X-User-Role`, а сервисы верили этим заголовкам на слово. Любой контейнер в сети мог прийти в
chat-svc или library-svc с чужим `X-User-Id` и действовать от имени кого угодно: безопасность
держалась на том, что порты сервисов не опубликованы. Теперь auth-svc подписывает короткоживущий
токен `X-User-Context`, nginx пробрасывает его вместо заголовков, а сервис проверяет подпись.

Токен — JWT (HS256) с утверждениями:
    iss  auth-svc
    aud  сервис-получатель (chat-svc, library-svc, auth-svc, db-svc)
    sub  идентификатор пользователя
    role роль пользователя
    iat, exp  выдан и истекает (60 секунд по умолчанию)

Ключ у каждого получателя СВОЙ: K(aud) = HMAC-SHA256(master, "user-context:v1:" + aud). Мастер-ключ
знает только auth-svc; получатель знает лишь собственный производный ключ. Поэтому
скомпрометированный chat-svc не может выпустить токен для library-svc или db-svc (а значит, и
выдать себя за администратора перед админкой) — он может лишь проверять токены, адресованные
ему. Дополнительно токен привязан к аудитории, живёт минуту и не годится для другого сервиса.

Файлы ключей: по одному шестнадцатеричному ключу в строке. Первый — текущий (им подписывают),
остальные принимаются при проверке: так делается ротация без простоя (scripts/gen_user_context_keys.py).

Модуль одинаков во всех сервисах-получателях (auth-svc, chat-svc, library-svc, db-svc); они
лежат в разных репозиториях и общего кода не имеют, менять его нужно во всех сразу. Подпись
используется только в auth-svc, остальные пользуются verify(). Только стандартная библиотека.

Переменные:
  USER_CONTEXT_AUDIENCE     имя этого сервиса как получателя (chat-svc, ...)
  USER_CONTEXT_KEY_FILE     файл с производным ключом получателя  (для получателей)
  USER_CONTEXT_MASTER_FILE  файл с мастер-ключом                  (только auth-svc)
  USER_CONTEXT_TTL_SEC      срок жизни токена, по умолчанию 60
  USER_CONTEXT_ALLOW_LEGACY true — принимать и голые X-User-Id / X-User-Role, если токена нет
                            (только на время перехода; по умолчанию запрещено)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

HEADER = "X-User-Context"
ISSUER = "auth-svc"
DEFAULT_TTL_SEC = 60
# Допуск на расхождение часов между контейнерами одного хоста: в норме их часы общие, но
# в Kubernetes между узлами они расходятся.
LEEWAY_SEC = 10

_KEY_LABEL = "user-context:v1:"


class UserContextError(Exception):
    """Токен отсутствует или не прошёл проверку."""


@dataclass(frozen=True)
class UserContext:
    user_id: str
    role: str
    audience: str
    expires_at: int


# ---------------------------------------------------------------------------
# Ключи
# ---------------------------------------------------------------------------

def derive_key(master: bytes, audience: str) -> bytes:
    return hmac.new(master, (_KEY_LABEL + audience).encode(), hashlib.sha256).digest()


_cache: dict[str, tuple[float, list[bytes]]] = {}


def load_keys(path: str | os.PathLike[str]) -> list[bytes]:
    """Читает ключи из файла (по одному в строке, hex). Перечитывается при изменении файла."""
    p = Path(path)
    if not p.is_file():
        # Типичная причина — bind-mount в compose на ещё не созданный файл: docker создаёт на его
        # месте каталог. Ключи выпускает scripts/gen_user_context_keys.py.
        raise UserContextError(
            f"файл ключа {p} не найден или не файл; создайте ключи: python3 scripts/gen_user_context_keys.py"
        )
    mtime = p.stat().st_mtime
    cached = _cache.get(str(p))
    if cached and cached[0] == mtime:
        return cached[1]
    keys: list[bytes] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            key = bytes.fromhex(line)
        except ValueError as exc:
            raise UserContextError(f"{p}: ключ должен быть шестнадцатеричной строкой") from exc
        if len(key) < 32:
            raise UserContextError(f"{p}: ключ короче 32 байт")
        keys.append(key)
    if not keys:
        raise UserContextError(f"{p}: нет ни одного ключа")
    _cache[str(p)] = (mtime, keys)
    return keys


# ---------------------------------------------------------------------------
# Токен
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(signing_input: bytes, key: bytes) -> bytes:
    return hmac.new(key, signing_input, hashlib.sha256).digest()


def sign(
    master_keys: list[bytes],
    audience: str,
    user_id: str,
    role: str,
    ttl_sec: int | None = None,
    now: float | None = None,
) -> str:
    """Выпускает токен для аудитории. Подписывает первым (текущим) мастер-ключом."""
    if not master_keys:
        raise UserContextError("нет мастер-ключа")
    issued = int(now if now is not None else time.time())
    ttl = ttl_sec if ttl_sec is not None else int(os.getenv("USER_CONTEXT_TTL_SEC", DEFAULT_TTL_SEC))
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"iss": ISSUER, "aud": audience, "sub": str(user_id), "role": role, "iat": issued, "exp": issued + ttl}
    signing_input = (
        _b64(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64(json.dumps(payload, separators=(",", ":")).encode())
    ).encode()
    signature = _sign(signing_input, derive_key(master_keys[0], audience))
    return signing_input.decode() + "." + _b64(signature)


def verify(token: str | None, audience: str, keys: list[bytes], now: float | None = None) -> UserContext:
    """Проверяет токен для аудитории. Ключи — производные ключи именно этого получателя."""
    if not token:
        raise UserContextError("нет заголовка " + HEADER)
    parts = token.split(".")
    if len(parts) != 3:
        raise UserContextError("токен не в формате JWT")
    try:
        header = json.loads(_unb64(parts[0]))
        payload = json.loads(_unb64(parts[1]))
        signature = _unb64(parts[2])
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise UserContextError("токен не разбирается") from exc

    # Алгоритм жёстко фиксирован: токен не выбирает, чем он проверяется (атака alg=none и подмена).
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise UserContextError("недопустимый алгоритм подписи")
    signing_input = (parts[0] + "." + parts[1]).encode()
    if not any(hmac.compare_digest(signature, _sign(signing_input, key)) for key in keys):
        raise UserContextError("неверная подпись")

    if not isinstance(payload, dict):
        raise UserContextError("недопустимое содержимое токена")
    if payload.get("iss") != ISSUER:
        raise UserContextError("недопустимый издатель")
    if payload.get("aud") != audience:
        raise UserContextError("токен выписан для другого сервиса")
    moment = now if now is not None else time.time()
    exp, iat = payload.get("exp"), payload.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int):
        raise UserContextError("нет срока действия")
    if exp < moment - LEEWAY_SEC:
        raise UserContextError("токен истёк")
    if iat > moment + LEEWAY_SEC:
        raise UserContextError("токен выдан в будущем")
    try:
        user_id = str(uuid.UUID(str(payload.get("sub"))))
    except ValueError as exc:
        raise UserContextError("недопустимый идентификатор пользователя") from exc
    role = payload.get("role")
    if not isinstance(role, str) or not role:
        raise UserContextError("нет роли")
    return UserContext(user_id=user_id, role=role, audience=audience, expires_at=exp)


# ---------------------------------------------------------------------------
# Удобные обёртки для сервисов: ключи и аудитория берутся из окружения
# ---------------------------------------------------------------------------

def allow_legacy() -> bool:
    return os.getenv("USER_CONTEXT_ALLOW_LEGACY", "").strip().lower() in ("1", "true", "yes")


def audience() -> str:
    value = os.getenv("USER_CONTEXT_AUDIENCE", "").strip()
    if not value:
        raise UserContextError("не задан USER_CONTEXT_AUDIENCE")
    return value


def verify_from_env(token: str | None) -> UserContext:
    """verify() с аудиторией и ключами из окружения (получатель)."""
    key_file = os.getenv("USER_CONTEXT_KEY_FILE", "").strip()
    if not key_file:
        raise UserContextError("не задан USER_CONTEXT_KEY_FILE")
    return verify(token, audience(), load_keys(key_file))


def check_configuration() -> None:
    """Проверка при запуске: получатель без ключа не должен молча принимать любые заголовки."""
    if allow_legacy():
        return
    audience()
    key_file = os.getenv("USER_CONTEXT_KEY_FILE", "").strip()
    if not key_file:
        raise UserContextError(
            "не задан USER_CONTEXT_KEY_FILE. Без ключа подпись пользовательского контекста "
            "проверить нельзя; для временной работы по старой схеме задайте USER_CONTEXT_ALLOW_LEGACY=true."
        )
    load_keys(key_file)
