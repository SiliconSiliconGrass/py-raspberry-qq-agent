from __future__ import annotations

from .contacts import ContactVault


class ChatAllowlist:
    """Hard allowlist for chats that may trigger or receive Agent actions."""

    def __init__(
        self,
        *,
        enabled: bool,
        private_qq_ids: list[str],
        group_ids: list[str],
        vault: ContactVault,
    ) -> None:
        self._enabled = enabled
        self._private_aliases = (
            {vault.encrypt("user", qq_id) for qq_id in private_qq_ids}
            if enabled
            else set()
        )
        self._group_aliases = (
            {vault.encrypt("group", group_id) for group_id in group_ids}
            if enabled
            else set()
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def allows(self, chat_type: str, encrypted_chat_id: str) -> bool:
        if not self._enabled:
            return True
        if chat_type == "private":
            return encrypted_chat_id in self._private_aliases
        if chat_type == "group":
            return encrypted_chat_id in self._group_aliases
        return False
