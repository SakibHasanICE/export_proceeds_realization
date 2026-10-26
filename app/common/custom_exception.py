import sys
import traceback


class CustomException(Exception):
    """Exception that carries the file/line where the underlying error happened."""

    def __init__(self, message: str, error_detail: Exception | None = None):
        self.error_message = self._build(message, error_detail)
        super().__init__(self.error_message)

    @staticmethod
    def _build(message: str, error_detail: Exception | None) -> str:
        _, _, tb = sys.exc_info()
        if error_detail is not None and error_detail.__traceback__ is not None:
            tb = error_detail.__traceback__
        if tb is None:
            return message
        last = traceback.extract_tb(tb)[-1]
        detail = f" | {type(error_detail).__name__}: {error_detail}" if error_detail else ""
        return f"{message} [{last.filename}:{last.lineno}]{detail}"

    def __str__(self) -> str:
        return self.error_message
