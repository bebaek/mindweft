from app.settings import MindweftSettings, MinigentSettings
from minigent_client.api_client import MindweftAPIClient, MinigentAPIClient
from minigent_client.errors import MindweftAPIError, MinigentAPIError
from minigent_client.runtime import MindweftClientRuntime, MinigentClientRuntime


def test_mindweft_public_python_names_are_canonical() -> None:
    assert MindweftSettings.__name__ == "MindweftSettings"
    assert MindweftAPIClient.__name__ == "MindweftAPIClient"
    assert MindweftAPIError.__name__ == "MindweftAPIError"
    assert MindweftClientRuntime.__name__ == "MindweftClientRuntime"


def test_minigent_public_python_names_remain_compatibility_aliases() -> None:
    assert MinigentSettings is MindweftSettings
    assert MinigentAPIClient is MindweftAPIClient
    assert MinigentAPIError is MindweftAPIError
    assert MinigentClientRuntime is MindweftClientRuntime
