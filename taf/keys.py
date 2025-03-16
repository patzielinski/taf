from functools import partial
from logging import INFO
from typing import Dict, List, Optional, Tuple, Union
import click
from pathlib import Path
from logdecorator import log_on_start
from taf.auth_repo import AuthenticationRepository
from taf.log import taf_logger
from taf.models.types import Role, RolesIterator
from taf.models.models import TAFKey
from taf.models.types import TargetsRole, MainRoles, UserKeyData
from taf.tuf.repository import MetadataRepository as TUFRepository, is_auth_repo
from taf.api.utils._conf import find_keystore
from taf.tuf.keys import (
    YkSigner,
    _get_legacy_keyid,
    generate_and_write_rsa_keypair,
    generate_rsa_keypair,
    get_sslib_key_from_value,
    load_signer_from_pem,
)

from taf.constants import DEFAULT_RSA_SIGNATURE_SCHEME, RoleSetupParams
from taf.exceptions import (
    KeystoreError,
    SigningError,
    YubikeyError,
)
from taf.keystore import (
    get_keystore_keys_of_role,
    key_cmd_prompt,
    load_signer_from_private_keystore,
)
from taf import YubikeyMissingLibrary
from securesystemslib.signer._crypto_signer import CryptoSigner

try:
    import taf.yubikey.yubikey as yk
except ImportError:
    taf_logger.warning(
        "WARNING: yubikey-manager dependency not installed. You will not be able to use YubiKeys."
    )
    yk = YubikeyMissingLibrary()  # type: ignore


def _create_signer(auth_repo, public_key, serial_num, key_name):
    return YkSigner(
        public_key,
        serial_num,
        partial(
            yk.yk_secrets_handler,
            pin_manager=auth_repo.pin_manager,
            serial_num=serial_num,
        ),
        key_name=key_name,
    )


def get_key_name(role_name: str, key_num: int, num_of_keys: int) -> str:
    """
    Return a keystore key's name based on the role's name and total number of signing keys,
    as well as the specified counter. If number of signing keys is one, return the role's name.
    If the number of signing keys is greater that one, return role's name + counter (root1, root2...)
    """
    if num_of_keys == 1:
        return role_name
    else:
        return role_name + str(key_num + 1)


def get_metadata_key_info(certs_dir: str, key_id: str) -> TAFKey:
    """
    Read and return information about the specified key read from a certificate
    file whose name matches that key's id.
    """
    cert_path = Path(certs_dir, key_id + ".cert")
    if cert_path.is_file():
        cert_pem = cert_path.read_bytes()
        return TAFKey(key_id, **_extract_x509(cert_pem))

    return TAFKey(key_id)


def _extract_x509(cert_pem: bytes) -> Dict:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend

    cert = x509.load_pem_x509_certificate(cert_pem, default_backend())

    def _get_attr(oid):
        attrs = cert.subject.get_attributes_for_oid(oid)
        return attrs[0].value if len(attrs) > 0 else ""

    return {
        "name": _get_attr(x509.OID_COMMON_NAME),
        "organization": _get_attr(x509.OID_ORGANIZATION_NAME),
        "country": _get_attr(x509.OID_COUNTRY_NAME),
        "state": _get_attr(x509.OID_STATE_OR_PROVINCE_NAME),
        "locality": _get_attr(x509.OID_LOCALITY_NAME),
        "valid_from": cert.not_valid_before.strftime("%Y-%m-%d"),
        "valid_to": cert.not_valid_after.strftime("%Y-%m-%d"),
    }


def load_sorted_keys_of_new_roles(
    roles: Union[MainRoles, TargetsRole],
    auth_repo: AuthenticationRepository,
    yubikeys_data: Optional[Dict[str, UserKeyData]],
    keystore: Optional[Union[Path, str]],
    existing_roles: Optional[List[str]] = None,
    skip_prompt: Optional[bool] = False,
    certs_dir: Optional[Union[Path, str]] = None,
):
    """
    Load signing keys of roles - first those stored on YubiKeys to avoid entering pins
    if there is something wrong with keystore files, then from keystore files.
    Recursively load keys of all delegated roles of target roles.

    Arguments:
        auth_repo: Authentication repository's instance
        roles: MainRoles object (including root, targets, snapshot and timestamp) or a particular delegated role
        yubikeys_data: Contains a mapping of a YubiKey's name to additional details. There names
            are used to link roles to YubiKeys that are used to sign the corresponding metadata.
            If additional details contain the public key, a user will not have to insert that YubiKey
            (provided that it's not necessary given the threshold of signing keys)
        keystore: keystore path
        pint_manager: Instance of a class for secure pin management
        existing_roles (optional): A list of roles whose keys were already loaded
        skip_prompt (optional): A flag defining if the user will be asked if they want to generate new keys or reuse existing
            ones in case keystore files should be used. New keys will be generated by default.
    Side Effects:
        Populates loaded YubiKeys database located in `yubikey.py`

    Returns:
        Signing and verification keys of roles
    """

    def _sort_roles(roles):
        keystore_roles = []
        yubikey_roles = []
        for role in RolesIterator(roles):
            if role.is_yubikey:
                yubikey_roles.append(role)
            else:
                keystore_roles.append(role)
        return keystore_roles, yubikey_roles

    # load and/or generate all keys first
    if existing_roles is None:
        existing_roles = []
    try:
        keystore_roles, yubikey_roles = _sort_roles(roles)
        signers: Dict = {}
        verification_keys: Dict = {}

        for role in keystore_roles:
            if role.name in existing_roles:
                continue
            keystore_signers, _, _ = setup_roles_keys(
                role,
                auth_repo,
                keystore=keystore,
                skip_prompt=skip_prompt,
            )
            for signer in keystore_signers:
                signers.setdefault(role.name, []).append(signer)

        for role in yubikey_roles:
            if role.name in existing_roles:
                continue
            _, yubikey_keys, yubikey_signers = setup_roles_keys(
                role,
                auth_repo,
                certs_dir=certs_dir,
                users_yubikeys_details=yubikeys_data,
                skip_prompt=skip_prompt,
            )
            verification_keys[role.name] = yubikey_keys
            signers[role.name] = yubikey_signers

        return signers, verification_keys
    except KeystoreError:
        raise SigningError("Could not load keys of new roles")


def _load_signer_from_keystore(
    taf_repo, keystore_path, key_name, num_of_signatures, scheme, role
) -> CryptoSigner:
    if keystore_path is None:
        return None
    if (keystore_path / key_name).is_file():
        try:
            signer = load_signer_from_private_keystore(
                keystore=keystore_path, key_name=key_name, scheme=scheme
            )
            # load only valid keys
            if taf_repo.is_valid_metadata_key(role, signer.public_key, scheme=scheme):
                return signer
        except KeystoreError:
            pass

    return None


def _load_yubikeys(
    taf_repo: TUFRepository,
    role: str,
    key_names: List[str],
    retry_on_failure: bool,
    hide_threshold_message: bool,
    key_id_pins: Optional[Dict] = None,
):
    """
    Loads YubiKeys for a specified role using given key names and manages the interaction process.

    Returns:
        A tuple containing two elements:
            - list of YkSigner instances representing the signers initialized from the loaded YubiKeys.
            - list of key names that were successfully loaded.
    """
    signers_yubikeys: List = []
    yubikeys = yk.yubikey_prompt(
        key_names=key_names,
        pin_manager=taf_repo.pin_manager,
        role=role,
        taf_repo=taf_repo,
        retry_on_failure=retry_on_failure,
        hide_already_loaded_message=True,
        hide_threshold_message=hide_threshold_message,
        key_id_pins=key_id_pins,
    )

    loaded_keyids = [signer.public_key.keyid for signer in signers_yubikeys]
    loaded_key_names = []
    for public_key, serial_num, key_name in yubikeys:
        if public_key is not None and public_key.keyid not in loaded_keyids:
            signer = YkSigner(
                public_key,
                serial_num,
                partial(
                    yk.yk_secrets_handler,
                    pin_manager=taf_repo.pin_manager,
                    serial_num=serial_num,
                ),
                key_name=key_name,
            )
            signers_yubikeys.append(signer)
            loaded_keyids.append(public_key.keyid)
            loaded_key_names.append(key_name)
            taf_logger.info(f"Successfully loaded {key_name} from inserted YubiKey")

    return signers_yubikeys, loaded_key_names


@log_on_start(INFO, "Loading signing keys of '{role:s}'", logger=taf_logger)
def load_signers(
    taf_repo: TUFRepository,
    role: str,
    keystore: Optional[str] = None,
    scheme: Optional[str] = DEFAULT_RSA_SIGNATURE_SCHEME,
    prompt_for_keys: Optional[bool] = False,
    key_id_pins: Optional[Dict] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Load role's signing keys. Make sure that at least the threshold of keys was
    loaded, but allow loading more keys (so that a metadata file can be signed
    by all of the role's keys if the user wants that)
    """
    threshold = taf_repo.get_role_threshold(role)
    signing_keys_num = len(taf_repo.get_role_keys(role))
    all_loaded = False
    num_of_signatures = 0
    signers_keystore: List = []
    signers_yubikeys: List = []

    # first try to sign using yubikey
    # if that is not possible, try to load key from a keystore file
    # if the keystore file is not found, ask the user if they want to sign
    # using yubikey and to insert it if that is the case

    if keystore is None:
        keystore_path = find_keystore(taf_repo.path)
        if keystore_path is None:
            taf_logger.warning("No keystore provided and no default keystore found")
        else:
            keystore = str(keystore_path)

    keystore_path = Path(keystore).expanduser().resolve() if keystore else None

    keystore_files = []
    if keystore is not None:
        keystore_files = get_keystore_keys_of_role(keystore, role)
    prompt_for_yubikey = True
    use_yubikey_for_signing_confirmed = False

    taf_repo.add_default_names_of_role(role)
    key_names = taf_repo.get_key_names_of_role(role)
    initial_yk_load_attempt = True
    hide_threshold_message = False

    while not all_loaded and num_of_signatures < signing_keys_num:

        # when loading from keystore files
        # there is no need to ask the user if they want to load more key, try to load from keystore
        if num_of_signatures < len(keystore_files):
            key_name = keystore_files[num_of_signatures]
            signer = _load_signer_from_keystore(
                taf_repo, keystore_path, key_name, num_of_signatures, scheme, role
            )
            if signer is not None:
                signers_keystore.append(signer)
                num_of_signatures += 1
                continue
        if num_of_signatures >= threshold:
            if (
                use_yubikey_for_signing_confirmed
                and not taf_repo.pin_manager.auto_continue
            ):
                if not click.confirm(
                    f"Threshold of {role} keys reached. Do you want to load more {role} keys?"
                ):
                    break
                else:
                    hide_threshold_message = True
            else:
                # loading from keystore files, couldn't load from all of them, but loaded enough
                # or auto continue sets
                break

        # try to load from the inserted YubiKeys, without asking the user to insert it
        # in case of yubikeys, instead of asking for a particular key, ask to insert all
        # that can be used to sign the current role, but either read the name from the
        # metadata, or assign a role + counter name
        if initial_yk_load_attempt or use_yubikey_for_signing_confirmed:
            loaded_signers, loaded_keys = _load_yubikeys(
                taf_repo=taf_repo,
                role=role,
                key_names=key_names,
                retry_on_failure=use_yubikey_for_signing_confirmed,
                hide_threshold_message=hide_threshold_message,
                key_id_pins=key_id_pins,
            )
            signers_yubikeys.extend(loaded_signers)
            if loaded_keys:
                use_yubikey_for_signing_confirmed = True
                num_of_loaded_keys = len(loaded_keys)
                for loaded_key in loaded_keys:
                    key_names.remove(loaded_key)

                if num_of_loaded_keys:
                    num_of_signatures += num_of_loaded_keys
                    continue

        if prompt_for_yubikey:
            if click.confirm(f"Sign {role} using YubiKey(s)?"):
                use_yubikey_for_signing_confirmed = True
                prompt_for_yubikey = False
                continue

        if prompt_for_keys and click.confirm(f"Manually enter {role} key?"):
            keys = [signer.public_key for signer in signers_keystore]
            key = key_cmd_prompt(key_name, role, taf_repo, keys, scheme)
            signer = load_signer_from_pem(key)
            signers_keystore.append(key)
            num_of_signatures += 1
        else:
            raise SigningError(f"Cannot load keys of role {role}")

    return signers_keystore, signers_yubikeys


def setup_roles_keys(
    role: Role,
    auth_repo: AuthenticationRepository,
    certs_dir: Optional[Union[Path, str]] = None,
    keystore: Optional[Union[Path, str]] = None,
    users_yubikeys_details: Optional[Dict[str, UserKeyData]] = None,
    skip_prompt: Optional[bool] = False,
    key_size: int = 2048,
):

    if role.name is None:
        raise SigningError("Cannot set up roles keys. Role name not specified")
    yubikey_keys = []
    keystore_signers = []
    yubikey_signers = []

    yubikey_ids = role.yubikey_ids
    if yubikey_ids is None:
        yubikey_ids = []
    if users_yubikeys_details is None:
        users_yubikeys_details = {}

    if yubikey_ids:
        if auth_repo.keys_name_mappings:
            # check if some of the listed key names are already defined as signing keys
            # in that case, they need to be loaded and verified
            if is_auth_repo(auth_repo.path):
                existing_key_names = {
                    existing_key_name: existing_key_id
                    for existing_key_id, existing_key_name in auth_repo.keys_name_mappings.items()
                }
                for key_name in yubikey_ids:
                    if key_name in existing_key_names:
                        public_key_pem, scheme = auth_repo.get_public_key_of_keyid(
                            existing_key_names[key_name]
                        )
                        key_data = {"public": public_key_pem, "scheme": scheme}
                        users_yubikeys_details[key_name] = UserKeyData(**key_data)

    if role.is_yubikey:
        yubikey_keys, yubikey_signers = _setup_yubikey_roles_keys(
            auth_repo, yubikey_ids, users_yubikeys_details, role, certs_dir, key_size
        )
    else:
        if keystore is None:
            taf_logger.error("No keystore provided and no default keystore found")
            raise KeyError("No keystore provided and no default keystore found")
        default_params = RoleSetupParams()
        for key_num in range(role.number):
            key_name = get_key_name(role.name, key_num, role.number)
            signer, key_id = _setup_keystore_key(
                keystore,
                role.name,
                key_name,
                role.scheme or default_params["scheme"],
                role.length or default_params["length"],
                None,
                skip_prompt=skip_prompt,
            )
            keystore_signers.append(signer)
            auth_repo.add_key_name(key_name, key_id)

    return keystore_signers, yubikey_keys, yubikey_signers


def _setup_yubikey_roles_keys(
    auth_repo, yubikey_ids, users_yubikeys_details, role, certs_dir, key_size
):
    loaded_keys_num = 0
    yk_with_public_key = {}
    yubikey_keys = []
    signers = []

    # if a key was already loaded (while setting up a different role)
    # register signers and remove the key id, so that the user is not asked to enter it again
    yubikes_to_skip = []
    names_defined = bool(yubikey_ids)
    if names_defined:
        for key_name in list(yubikey_ids):
            key_data = auth_repo.yubikey_store.get_key_data(key_name)
            if key_data is not None:
                public_key, serial_num = key_data
                auth_repo.yubikey_store.add_key_data(
                    key_name, serial_num, public_key, role.name
                )
                yubikey_ids.remove(key_name)
                yubikey_keys.append(public_key)
                loaded_keys_num += 1
                signer = _create_signer(auth_repo, public_key, serial_num, key_name)
                signers.append(signer)

        # if key already loaded while setting up a different role, skip it
        # if the current role's yubikey ids are defined
        # however, if the current role's yubikey ids are not specified
        # it can be possible to reuse a yubikey
        for key_name, key_data in auth_repo.yubikey_store.yubikeys_data.items():
            if key_name not in yubikey_ids:
                yubikes_to_skip.append(key_data["serial"])
    else:
        yubikey_ids = [f"{role.name}{counter}" for counter in range(1, role.number + 1)]

    for key_name in yubikey_ids:
        public_key_text = None
        if key_name in users_yubikeys_details:
            public_key_text = users_yubikeys_details[key_name].public
        if public_key_text:
            scheme = users_yubikeys_details[key_name].scheme
            public_key = get_sslib_key_from_value(public_key_text, scheme)
            # Check if the signing key is already loaded
            if not auth_repo.yubikey_store.is_key_name_loaded(key_name):
                yk_with_public_key[key_name] = public_key
            else:
                serial_num = auth_repo.yubikey_store.get_key_data["serial"]
                auth_repo.yubikey_store.add_key_data(
                    key_name, serial_num, public_key, role.name
                )
                loaded_keys_num += 1
            yubikey_keys.append(public_key)
        else:
            key_scheme = None
            if key_name in users_yubikeys_details:
                key_scheme = users_yubikeys_details[key_name].scheme
            key_scheme = key_scheme or role.scheme
            public_key, serial_num = _setup_yubikey(
                auth_repo,
                role.name,
                key_name,
                key_scheme,
                certs_dir,
                key_size,
                yubikes_to_skip,
            )
            loaded_keys_num += 1
            signer = _create_signer(auth_repo, public_key, serial_num, key_name)
            signers.append(signer)

        key_id = _get_legacy_keyid(public_key)
        auth_repo.add_key_name(key_name, key_id, overwrite=names_defined)

    if loaded_keys_num < role.number:
        if loaded_keys_num < role.threshold:
            print(f"Threshold of role {role.name} is {role.threshold}")

        _load_remaining_keys_of_role(
            auth_repo,
            role,
            loaded_keys_num,
            users_yubikeys_details,
            yk_with_public_key,
            yubikey_keys,
            signers,
        )
    return yubikey_keys, signers


def _setup_keystore_key(
    keystore: Optional[Union[Path, str]],
    role_name: str,
    key_name: str,
    scheme: str,
    length: int,
    password: Optional[str],
    skip_prompt: Optional[bool],
) -> Tuple[CryptoSigner, str]:
    # if keystore exists, load the keys
    generate_new_keys = keystore is None
    signer = None

    def _invalid_key_message(key_name, keystore):
        key_path = Path(keystore, key_name)
        if key_path.is_file():
            print(f"Could not load private key {key_path}")
        else:
            print(f"{key_path} is not a file!")

    if keystore is not None:
        keystore_path = str(Path(keystore).expanduser().resolve())
        while signer is None:
            try:
                signer = load_signer_from_private_keystore(
                    keystore_path,
                    key_name,
                    scheme=scheme,
                    password=password,
                )
            except KeystoreError:
                _invalid_key_message(key_name, keystore)

            if signer is None:
                generate_new_keys = skip_prompt is True or click.confirm(
                    "Generate new keys?"
                )
                if not generate_new_keys:
                    if click.confirm("Reuse existing key?"):
                        reused_key_name = input(
                            "Enter name of an existing keystore file: "
                        )
                        # copy existing private and public keys to the new files
                        Path(keystore, key_name).write_bytes(
                            Path(keystore, reused_key_name).read_bytes()
                        )
                        Path(keystore, key_name + ".pub").write_bytes(
                            Path(keystore, reused_key_name + ".pub").read_bytes()
                        )
                    else:
                        raise KeystoreError(f"Could not load {key_name}")
                else:
                    break
    if generate_new_keys:
        if keystore is not None and (
            skip_prompt or click.confirm("Write keys to keystore files?")
        ):
            if password is None and not skip_prompt:
                password = input(
                    "Enter keystore password and press ENTER (can be left empty)"
                )
            private_pem = generate_and_write_rsa_keypair(
                path=Path(keystore, key_name), key_size=length, password=password
            )
            signer = load_signer_from_pem(private_pem)
        else:
            _, private_pem = generate_rsa_keypair(key_size=length)
            print(f"{role_name} key:\n\n{private_pem.decode()}\n\n")
            signer = load_signer_from_pem(private_pem)

    if signer is not None:
        return signer, _get_legacy_keyid(signer.public_key)
    raise KeystoreError(f"Could not load signer {key_name}")


def _setup_yubikey(
    auth_repo: AuthenticationRepository,
    role_name: str,
    key_name: str,
    scheme: Optional[str] = DEFAULT_RSA_SIGNATURE_SCHEME,
    certs_dir: Optional[Union[Path, str]] = None,
    key_size: int = 2048,
    yubikeys_to_skip: Optional[List] = None,
) -> Tuple[Dict, str]:
    print(f"Registering keys for {key_name}")
    while True:
        use_existing = click.confirm("Do you want to reuse already set up Yubikey?")
        if not use_existing:
            if not click.confirm(
                "WARNING - this will delete everything from the inserted key. Proceed?"
            ):
                if click.confirm("Cancel?"):
                    raise YubikeyError("Yubikey setup canceled")
                continue
        yubikeys = yk.yubikey_prompt(
            [key_name],
            pin_manager=auth_repo.pin_manager,
            role=role_name,
            taf_repo=auth_repo,
            registering_new_key=True,
            creating_new_key=not use_existing,
            pin_confirm=True,
            pin_repeat=True,
            yubikeys_to_skip=yubikeys_to_skip,
        )
        if yubikeys is not None:
            key, serial_num, key_name = yubikeys[0]
            if not use_existing:
                key = yk.setup_new_yubikey(
                    auth_repo.pin_manager, serial_num, scheme, key_size=key_size
                )

            if certs_dir is not None:
                # check if already exporeted
                if len(auth_repo.yubikey_store.get_roles_of_key(serial_num)) == 1:
                    # this is the first time that this key is being used (can only be used once per role)
                    yk.export_yk_certificate(certs_dir, key, serial=serial_num)
            return key, serial_num


def _load_remaining_keys_of_role(
    auth_repo: AuthenticationRepository,
    role: Role,
    loaded_keys_num: int,
    users_yubikeys_details: UserKeyData,
    yk_with_public_key: Dict,
    yubikey_keys,
    signers: List,
):
    """
    If a a yubikey's public key was specified, meaning that it can be added as a
    verification key without being inserted, but the total number of signing
    keys is smaller than the threshold
    """
    while loaded_keys_num < role.threshold:
        loaded_keys = []
        for key_name, public_key in yk_with_public_key.items():
            serial_num = _load_and_verify_yubikey(
                role.name,
                key_name,
                public_key,
                taf_repo=auth_repo,
            )
            if serial_num:
                loaded_keys_num += 1
                loaded_keys.append(key_name)
                signer = _create_signer(auth_repo, public_key, serial_num, key_name)
                signers.append(signer)
                yubikey_keys.remove(signer.public_key)

        if loaded_keys_num < role.threshold:
            if not click.confirm(
                f"Threshold of signing keys of role {role.name} not reached. Continue?"
            ):
                raise SigningError("Not enough signing keys")
            for key_name in loaded_keys:
                yk_with_public_key.pop(key_name)


def _load_and_verify_yubikey(
    role_name: str,
    key_name: str,
    public_key,
    taf_repo: AuthenticationRepository,
) -> Optional[str]:
    if not click.confirm(f"Sign using {key_name} Yubikey?"):
        return None
    while True:
        yubikeys = yk.yubikey_prompt(
            [key_name],
            role=role_name,
            pin_manager=taf_repo.pin_manager,
            taf_repo=taf_repo,
            registering_new_key=True,
            creating_new_key=False,
            pin_confirm=True,
            pin_repeat=True,
        )
        if yubikeys:
            yubikey = yubikeys[0]
            yk_pub_key_id = yubikey[0].keyid
            if yk_pub_key_id != public_key.keyid:
                print(
                    "Public key of the inserted key is not equal to the specified one."
                )
                if not click.confirm("Try again?"):
                    return None
            return yubikey[1]
