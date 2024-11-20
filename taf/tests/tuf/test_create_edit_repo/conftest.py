from taf.models.converter import from_dict
from taf.models.types import RolesKeysData
from taf.tuf.repository import MetadataRepository
import pytest


@pytest.fixture(autouse=False)
def tuf_repo(tuf_repo_path, signers_with_delegations, with_delegations_no_yubikeys_input):
    repo = MetadataRepository(tuf_repo_path)
    roles_keys_data = from_dict(with_delegations_no_yubikeys_input, RolesKeysData)
    repo.create(roles_keys_data, signers_with_delegations)
    yield repo
