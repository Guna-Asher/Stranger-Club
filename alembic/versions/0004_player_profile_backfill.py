"""legacy Player -> PlayerProfile/User backfill

Data-only migration. For every legacy `players` row (phone-keyed, pre-auth
identity), if a `users` row with the same phone already exists (i.e. that
person has since completed OTP verification), copy the cached name/role into
their PlayerProfile and email into their User record — but only where those
fields are still unset, never overwriting anything already there.

Deliberately NOT done: creating a User for a legacy phone that never
verified. That would invent an authenticated identity for someone who never
authenticated. Those phones' cached name/role default is not carried
forward; every individual Registration row keeps its own immutable snapshot
regardless, so no *registration* history is affected by this gap.

The `players` table itself is left completely intact — this migration only
reads from it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    legacy_players = bind.execute(sa.text("SELECT phone, name, preferred_position, email FROM players")).mappings().all()

    migrated = 0
    skipped_no_user = 0
    for row in legacy_players:
        user_id = bind.execute(sa.text("SELECT id FROM users WHERE phone = :phone"), {"phone": row["phone"]}).scalar_one_or_none()
        if user_id is None:
            skipped_no_user += 1
            continue

        profile = bind.execute(
            sa.text("SELECT display_name, cricket_role FROM player_profiles WHERE user_id = :uid"), {"uid": user_id}
        ).mappings().first()
        if profile is None:
            # Should not happen: verify_otp always creates a profile row for
            # a new user. Nothing to backfill onto if it's somehow missing.
            continue

        updates: dict[str, str] = {}
        if not profile["display_name"] and row["name"]:
            updates["display_name"] = row["name"]
        if (not profile["cricket_role"] or profile["cricket_role"] == "NO_PREFERENCE") and row["preferred_position"]:
            updates["cricket_role"] = row["preferred_position"]
        if updates:
            set_clause = ", ".join(f"{key} = :{key}" for key in updates)
            bind.execute(sa.text(f"UPDATE player_profiles SET {set_clause} WHERE user_id = :uid"), {**updates, "uid": user_id})

        if row["email"]:
            bind.execute(sa.text("UPDATE users SET email = :email WHERE id = :uid AND email IS NULL"), {"email": row["email"], "uid": user_id})

        migrated += 1

    print(f"[0004_player_profile_backfill] migrated={migrated} skipped_no_matching_user={skipped_no_user}")


def downgrade() -> None:
    # Data-only migration; nothing to structurally revert. Backfilled profile
    # fields are intentionally left as-is (downgrading a schema version does
    # not un-fill a display name a real person may now be relying on).
    pass
