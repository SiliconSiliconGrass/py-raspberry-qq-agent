from py_raspberry_qq_agent.config import _default_system_prompt


def test_tree_raspberry_prompt_keeps_persona_and_removes_catchphrases() -> None:
    prompt = _default_system_prompt()

    assert "树莓娘" in prompt
    assert "网络开拓者协会" in prompt
    assert "QQ 回复默认简短" in prompt
    assert "send_qq_message" in prompt
    assert "delete_todo_item" in prompt
    assert "Ciallo" not in prompt
    assert "强强" not in prompt
    assert "唐完了" not in prompt
