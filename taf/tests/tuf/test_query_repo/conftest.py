import shutil
from taf.tuf.repository import MetadataRepository
from taf.utils import on_rm_error
import pytest
from taf.models.types import RolesKeysData
from taf.models.converter import from_dict


@pytest.fixture(scope="module")
def tuf_repo_no_delegations(tuf_repo_path, signers, no_yubikeys_input):
    # Create new metadata repository
    path = tuf_repo_path / "repository_without_delegations"
    path.mkdir(parents=True)
    tuf_repo = MetadataRepository(path)
    roles_keys_data = from_dict(no_yubikeys_input, RolesKeysData)
    tuf_repo.create(roles_keys_data, signers)

    tuf_repo.add_target_files_to_role(
        {"test1.txt": {"target": "test1"}, "test2.txt": {"target": "test2"}}
    )
    yield tuf_repo
    shutil.rmtree(path, onerror=on_rm_error)


@pytest.fixture(scope="module")
def tuf_repo_with_delegations(
    tuf_repo_path, signers_with_delegations, with_delegations_no_yubikeys_input
):
    # Create new metadata repository
    path = tuf_repo_path / "repository_with_delegations"
    path.mkdir(parents=True)
    tuf_repo = MetadataRepository(path)
    roles_keys_data = from_dict(with_delegations_no_yubikeys_input, RolesKeysData)
    tuf_repo.create(roles_keys_data, signers_with_delegations)

    # targets role's targets
    target_path1 = "test1"
    target_path2 = "test2"
    tuf_repo.add_target_files_to_role(
        {target_path1: {"target": "test1"}, target_path2: {"target": "test2"}}
    )
    delegated_path1 = "dir1/path1"
    delegated_path2 = "dir2/path1"
    custom1 = {"custom_attr1": "custom_val1"}
    custom2 = {"custom_attr2": "custom_val2"}

    tuf_repo.add_target_files_to_role(
        {
            delegated_path1: {"target": "test1", "custom": custom1},
            delegated_path2: {"target": "test2", "custom": custom2},
        }
    )

    path_delegated = "dir2/path2"
    tuf_repo.add_target_files_to_role(
        {
            path_delegated: {"target": "test3"},
        }
    )
    yield tuf_repo
    shutil.rmtree(path, onerror=on_rm_error)
