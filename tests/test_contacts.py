from pathlib import Path

from py_raspberry_qq_agent.contacts import ContactDirectory, ContactVault, RoleStore


def test_contact_alias_is_stable_encrypted_and_resolvable(tmp_path: Path) -> None:
    path = tmp_path / "contacts.json"
    vault = ContactVault(path, "a sufficiently long local secret")
    raw = "123456789"

    alias = vault.encrypt("user", raw)
    assert raw not in alias
    assert alias.startswith("qq-user:")
    assert vault.encrypt("user", raw) == alias
    assert vault.resolve(alias) == ("user", raw)

    reopened = ContactVault(path, "a sufficiently long local secret")
    assert reopened.resolve(alias) == ("user", raw)


def test_profiles_and_roles_are_separate(tmp_path: Path) -> None:
    vault = ContactVault(tmp_path / "contacts.json", "secret")
    alias = vault.encrypt("user", "10001")
    profiles = ContactDirectory(tmp_path / "profiles.json")
    roles = RoleStore(tmp_path / "roles.json")

    profiles.update(alias, "Known user", "Helps with events")
    assert profiles.get(alias)["display_name"] == "Known user"
    assert roles.role_of(alias) == "user"

    roles.set_admin(alias, True)
    assert roles.is_admin(alias)
    assert profiles.get(alias)["notes"] == "Helps with events"
