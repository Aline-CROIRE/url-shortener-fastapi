import secrets
import string

ALPHABET = string.ascii_letters + string.digits  # a-z, A-Z, 0-9 (62 characters)
CODE_LENGTH = 7


def generate_code(length: int = CODE_LENGTH) -> str:
    # secrets uses a cryptographically secure random source, so codes cannot be guessed
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
