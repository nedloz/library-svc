from uuid import UUID

from fastapi import Header, HTTPException, status

from app import user_context


def get_gateway_user_id(
    x_user_context: str | None = Header(
        default=None,
        alias=user_context.HEADER,
    ),
    x_user_id: str | None = Header(
        default=None,
        alias="X-User-Id",
    ),
) -> UUID:
    """
    Пользователь из подписанного контекста X-User-Context.

    nginx после проверки JWT через auth-svc получает от него токен, адресованный library-svc,
    и пробрасывает его. library-svc проверяет подпись (app/user_context.py), а не верит
    голому заголовку X-User-Id, который мог выставить любой контейнер сети.

    Возвращает UUID пользователя.
    """

    if x_user_context:
        try:
            return UUID(user_context.verify_from_env(x_user_context).user_id)
        except user_context.UserContextError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user context",
            ) from exc

    # Переходный режим (USER_CONTEXT_ALLOW_LEGACY): прежний заголовок без подписи.
    if x_user_id and user_context.allow_legacy():
        try:
            return UUID(x_user_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid X-User-Id",
            ) from exc

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
    )
