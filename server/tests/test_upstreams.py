import uuid

import pytest

from trimset_server.upstreams import upstreams_from_env


def test_upstreams_come_from_the_environment() -> None:
    pid = str(uuid.uuid4())
    found = upstreams_from_env(
        {
            "TRIMSET_UPSTREAM_JEV_URL": "https://api.typesafe.ai/",
            "TRIMSET_UPSTREAM_JEV_KEY": "sk-secret",
            "TRIMSET_UPSTREAM_OLLAYA_LOCAL_URL": "http://ollaya:11435",
            "TRIMSET_UPSTREAM_OPENAI_URL": "https://api.openai.com",
            "TRIMSET_UPSTREAM_OPENAI_FORMAT": "openai-decisions",
            "TRIMSET_UPSTREAM_OPENAI_PROJECTS": f" {pid.upper()} , other",
            "UNRELATED": "x",
        }
    )
    assert sorted(found) == ["jev", "ollaya_local", "openai"]
    assert found["jev"].url == "https://api.typesafe.ai"
    assert found["jev"].key == "sk-secret"
    assert "sk-secret" not in repr(found["jev"])  # a key never shows in logs or errors
    assert found["ollaya_local"].format == "systemone"
    assert found["openai"].format == "openai-decisions"
    assert found["openai"].allows(uuid.UUID(pid))
    assert not found["openai"].allows(uuid.uuid4())
    assert found["jev"].allows(uuid.uuid4())


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"TRIMSET_UPSTREAM_X_FORMAT": "systemone"}, "URL is not set"),
        ({"TRIMSET_UPSTREAM_X_URL": "ftp://host"}, "http or https"),
        ({"TRIMSET_UPSTREAM_X_URL": "https://u:p@host"}, "credentials"),
        ({"TRIMSET_UPSTREAM_X_URL": "https://host?x=1"}, "query"),
        ({"TRIMSET_UPSTREAM_X_URL": "https://host", "TRIMSET_UPSTREAM_X_FORMAT": "soap"}, "FORMAT"),
        (
            {"TRIMSET_UPSTREAM_X_URL": "http://host", "TRIMSET_UPSTREAM_X_KEY": "k"},
            "must use https",
        ),
    ],
)
def test_misconfigured_upstreams_stop_the_server(env: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        upstreams_from_env(env)
