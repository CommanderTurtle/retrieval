from pathlib import Path
from types import SimpleNamespace

from hermes_retrieval.scout import RetrievalScout, _extract_json


def test_scout_json_parser_accepts_plain_and_fenced_objects() -> None:
    assert _extract_json('{"selected_id":null,"reason":"none"}')["selected_id"] is None
    fence = chr(96) * 3
    parsed = _extract_json(
        f"{fence}json\n"
        '{"selected_id":"repo:alpha","reason":"matches"}\n'
        f"{fence}"
    )
    assert parsed["selected_id"] == "repo:alpha"


def test_scout_uses_private_home_and_explicit_agent_directory(tmp_path: Path) -> None:
    omp_config = tmp_path / ".omp" / "agent" / "config.yml"
    profile = tmp_path / ".omp" / "profiles" / "retrieval" / "agent"
    profile.mkdir(parents=True)
    (profile / ".retrieval-scout.json").write_text("{}", encoding="utf-8")
    settings = SimpleNamespace(
        scout_profile="retrieval",
        scout_home=tmp_path / "scout-home",
        omp_config=omp_config,
        omp_command="/opt/omp",
        scout_timeout=120,
        scout_model="",
        catalog_root=tmp_path / "catalog",
    )
    scout = RetrievalScout(settings)

    environment = scout._environment()
    command = scout._command()

    assert environment["HOME"] == str(tmp_path / "scout-home")
    assert environment["PI_CODING_AGENT_DIR"] == str(profile)
    assert not any(argument.startswith("--profile=") for argument in command)
