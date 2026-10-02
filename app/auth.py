from uuid import UUID

from fastapi import Header, HTTPException, status


def get_gateway_user_id(
    x_user_id: str | None = Header(
        default=None,
        alias="X-User-Id",
    ),
) -> UUID:
    """
    Проверяет X-User-Id, который передаёт nginx
    после успешной проверки JWT через auth-svc.

    library-svc не проверяет JWT самостоятельно.
    JWT уже проверен gateway.

    Возвращает UUID пользователя.
    """

    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )

    try:
        return UUID(x_user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid X-User-Id",
        ) from exc
