from hermes_retrieval.server import mcp


def test_mcp_surface_is_retrieval_only() -> None:
    tools = getattr(mcp._tool_manager, "_tools")

    assert set(tools) == {"retrieve_skill", "retrieve_reference"}
