"""Test YkSigner"""

from functools import partial
import os

from taf.yubikey.yubikey_manager import PinManager
import pytest

from securesystemslib.exceptions import UnverifiedSignatureError


# Test data to sign
_DATA = b"DATA"
_NOT_DATA = b"NOT DATA"

# Test public key
# >>>  with open("taf/tests/data/keystores/keystore/root1.pub", "rb") as f:
# >>>      _PUB = f.read()
_PUB = b"-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA5EGVh9xqVFFHnGGIofks\ncA3vHWFs1QP60QTX+ZJUPiUJdDb8wuJ6mu9d8bKojE3SEVHCLpJeV4+muMnLtZWq\nAipiuFUU9QDpOYaqQ5SD5n/9sZfiWDzjVsqZA4WMj0OCd/Bkn+umz3ljHFe0EJUE\nCxYRvmArC05UyJej7fCaQ/cD7QELrpmBaE2qLcG0Vfirz9NekaXixGiKNiIjHAj6\nYwIfES9SycVo42LEOskGFciqgfZJVtSaTIurW+KnOToStazEWY8okon91s+5ltIN\nOS68TtBLtph5PXcLhqSozE8SqMW3gZni6zXHHQtuouFLdGkgw+0V2YLX15Ka78zj\nhQIDAQAB\n-----END PUBLIC KEY-----"

# Test signature
# >>> signer = load_signer_from_file("taf/tests/data/keystores/keystore/root1", None)
# >>> sig = signer.sign(_DATA)
# >>> _SIG = bytes.fromhex(sig.signature)
_SIG = b"\xc1}\xaa\xec\xf6#;\xe6\x89\xc26\x81\x1a;\xd3\xb2\x7f\xce\xe3}\x9a6w}P\xe0d\x8d\xeb\xbcb\xba8\x8c\x96tS\xf2_\xf37\xe8Z\xc4\xf4\x1a\xaa\xdd\xdd%AB#w\x93\xc9\x0f\x8d\xe4\x93)\x9f\xa4)\x0b\xbb\xce\xf4\x9e\x8b\xaa\x1c\xda\xb8\x9ex\xe2\xc8\x9c\x02\\\xb7\x89\x88g\xd3\xb2\x0be\xf4S\x0c*\x0c\xce\xfe\x8aL=\x07\xfa\xe9\xa2\xe1\xed\x1cA\xf9\xbeZR\x91\xae@\x12\xfe<n\xe9;\xa3\xcdr\xabB\x87\x02N\xe5\x8a\x0b3>\xbey`\x07 /)Z_\xd0\xca\x7f\xcey\xe6\x1ee~\x01\x0c\xcfQZ=a\xf6\xe9\xabm_\x12\x8e\xda\xb0\xd4\xaeb1W\x0e\xf0\x909\xae\x05}\x8f\xba\xf7\xa0\\Rx\xe9\x98\x0f4j86\x87\x17\xf5\xff\xc2U\x80oh\xad\xb2\xaf\xa5\x91\x9a\xafI,\xadj\xd5\x02$\xc6\xf8\xf2`y\xd2\xa6\xf3\xce[;\r\xb6y\xd4\xa5\x96y$}{!r\xc1\xfb@\x1e<\xd9\xa0\xe6\x7f\xf1\x17\xe5\x0c\x8e\xbd\xf3\xba"


def is_yubikey_manager_installed():
    try:
        import taf.yubikey  # noqa: F401
    except ImportError:
        return False
    else:
        return True


@pytest.mark.skipif(
    not is_yubikey_manager_installed(),
    reason="Yubikey Manager not installed)",
)
def test_fake_yk(mocker):
    """Test public key export and signing with fake Yubikey."""
    mocker.patch("taf.yubikey.yubikey.export_piv_pub_key", return_value=_PUB)
    mocker.patch("taf.yubikey.yubikey.sign_piv_rsa_pkcs1v15", return_value=_SIG)
    mocker.patch("taf.yubikey.yubikey.verify_yk_inserted", return_value=True)
    mocker.patch("taf.yubikey.yubikey.get_serial_nums", return_value=["1234"])

    from taf.tuf.keys import YkSigner

    key = YkSigner.import_()

    def _secrets_handler(key_name):
        return "123456"

    signer = YkSigner(key, "1234", _secrets_handler, "test")

    sig = signer.sign(_DATA)
    key.verify_signature(sig, _DATA)
    with pytest.raises(UnverifiedSignatureError):
        key.verify_signature(sig, _NOT_DATA)


@pytest.mark.skipif(
    not os.environ.get("REAL_YK"),
    reason="Run test with REAL_YK=1 (test will prompt for pin)",
)
def test_real_yk():
    """Test public key export and signing with real Yubikey."""

    import taf.yubikey.yubikey as yk
    from taf.tuf.keys import YkSigner
    from taf.yubikey.yubikey import get_serial_nums

    serials = get_serial_nums()
    serial = serials[0]
    pin_manager = PinManager()
    pin_manager.add_pin(serial, "123456")

    secrets_handler = partial(
        yk.yk_secrets_handler,
        pin_manager=pin_manager,
        serial_num=serial,
    )
    key = YkSigner.import_()
    signer = YkSigner(key, serial, secrets_handler, "test")

    sig = signer.sign(_DATA)
    key.verify_signature(sig, _DATA)
    with pytest.raises(UnverifiedSignatureError):
        key.verify_signature(sig, _NOT_DATA)
