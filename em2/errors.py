"""Failure categories shared by the executor, service and user interface."""


class ParameterError(ValueError):
    kind = "parameter_error"


class ConfigurationError(RuntimeError):
    kind = "configuration_error"


class ProtectionError(RuntimeError):
    kind = "pump_fault"


class AccountingError(RuntimeError):
    kind = "accounting_error"


class CommunicationError(ValueError):
    kind = "communication_error"


def failure_kind(error):
    if hasattr(error, "kind"):
        return error.kind
    if isinstance(error, OSError):  # Includes serial.SerialException and TimeoutError.
        return "communication_error"
    return "software_error"
