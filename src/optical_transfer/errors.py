"""Errors safe to report at the command line."""


class TransferError(Exception):
    """An invalid transfer, unsupported input, or failed operation."""


class InvalidPacket(TransferError):
    """A QR payload that must not contribute to recovery."""
