from datetime import datetime

from beanie import PydanticObjectId

from users_service.auth.model import RefreshToken


async def create_refresh_token(user_id: PydanticObjectId, token_hash: str, expires_at: datetime) -> RefreshToken:
    record = RefreshToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    await record.insert()
    return record


async def get_valid_by_hash(token_hash: str) -> RefreshToken | None:
    return await RefreshToken.find_one(RefreshToken.token_hash == token_hash, RefreshToken.revoked == False)  # noqa: E712


async def revoke(record: RefreshToken) -> None:
    record.revoked = True
    await record.save()


async def revoke_all_for_user(user_id: PydanticObjectId) -> None:
    await RefreshToken.find(RefreshToken.user_id == user_id, RefreshToken.revoked == False).update(  # noqa: E712
        {"$set": {"revoked": True}}
    )
