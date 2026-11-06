"""Stable business errors shared by the wire, workers and Vietnamese UI."""
from enum import Enum


class ErrorCode(str, Enum):
    BAD_FRAME = "BAD_FRAME"
    HEADER_TOO_LARGE = "HEADER_TOO_LARGE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    PAYLOAD_LENGTH_MISMATCH = "PAYLOAD_LENGTH_MISMATCH"
    UNEXPECTED_EOF = "UNEXPECTED_EOF"
    BAD_JSON = "BAD_JSON"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_FIELD = "INVALID_FIELD"
    SPOOFED_FIELD = "SPOOFED_FIELD"
    BAD_PAYLOAD = "BAD_PAYLOAD"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    AUTH_FAILED = "AUTH_FAILED"
    ALREADY_LOGGED_IN = "ALREADY_LOGGED_IN"
    RECIPIENT_OFFLINE = "RECIPIENT_OFFLINE"
    RECIPIENT_UNKNOWN = "RECIPIENT_UNKNOWN"
    QUEUE_FULL = "QUEUE_FULL"
    TIMEOUT = "TIMEOUT"
    CONNECTION_LOST = "CONNECTION_LOST"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    UNSAFE_FILENAME = "UNSAFE_FILENAME"
    TRANSFER_UNKNOWN = "TRANSFER_UNKNOWN"
    TRANSFER_STATE = "TRANSFER_STATE"
    SEQ_MISMATCH = "SEQ_MISMATCH"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    HASH_MISMATCH = "HASH_MISMATCH"
    DISK_FULL = "DISK_FULL"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    REJECTED_BY_PEER = "REJECTED_BY_PEER"
    CANCELLED_BY_PEER = "CANCELLED_BY_PEER"
    CANCELLED_LOCAL = "CANCELLED_LOCAL"
    INTERNAL = "INTERNAL"


USER_MESSAGES = {
    ErrorCode.BAD_FRAME: "Khung dữ liệu không hợp lệ.",
    ErrorCode.HEADER_TOO_LARGE: "Phần đầu dữ liệu quá lớn.",
    ErrorCode.PAYLOAD_TOO_LARGE: "Dữ liệu vượt giới hạn cho phép.",
    ErrorCode.PAYLOAD_LENGTH_MISMATCH: "Độ dài dữ liệu không khớp.",
    ErrorCode.UNEXPECTED_EOF: "Kết nối đóng khi dữ liệu chưa đầy đủ.",
    ErrorCode.BAD_JSON: "Dữ liệu JSON không hợp lệ.",
    ErrorCode.UNSUPPORTED_VERSION: "Phiên bản giao thức chưa được hỗ trợ.",
    ErrorCode.UNKNOWN_TYPE: "Loại thông điệp không được hỗ trợ.",
    ErrorCode.MISSING_FIELD: "Thiếu trường dữ liệu bắt buộc.",
    ErrorCode.INVALID_FIELD: "Trường dữ liệu không hợp lệ.",
    ErrorCode.SPOOFED_FIELD: "Thông tin người gửi không hợp lệ.",
    ErrorCode.BAD_PAYLOAD: "Nội dung dữ liệu không hợp lệ.",
    ErrorCode.NOT_AUTHENTICATED: "Bạn cần đăng nhập trước.",
    ErrorCode.AUTH_FAILED: "Tên đăng nhập hoặc mật khẩu không đúng.",
    ErrorCode.ALREADY_LOGGED_IN: "Tài khoản đang đăng nhập ở nơi khác.",
    ErrorCode.RECIPIENT_OFFLINE: "Người nhận đang ngoại tuyến.",
    ErrorCode.RECIPIENT_UNKNOWN: "Không tìm thấy người nhận.",
    ErrorCode.QUEUE_FULL: "Hàng đợi đã đầy. Vui lòng thử lại.",
    ErrorCode.TIMEOUT: "Hết thời gian chờ phản hồi.",
    ErrorCode.CONNECTION_LOST: "Đã mất kết nối.",
    ErrorCode.FILE_TOO_LARGE: "File vượt kích thước cho phép.",
    ErrorCode.FILE_NOT_FOUND: "Không tìm thấy file.",
    ErrorCode.SOURCE_CHANGED: "File nguồn đã thay đổi khi đang gửi.",
    ErrorCode.UNSAFE_FILENAME: "Tên file không an toàn.",
    ErrorCode.TRANSFER_UNKNOWN: "Không tìm thấy phiên truyền file.",
    ErrorCode.TRANSFER_STATE: "Trạng thái truyền file không hợp lệ.",
    ErrorCode.SEQ_MISMATCH: "Thứ tự khối dữ liệu không đúng.",
    ErrorCode.SIZE_MISMATCH: "Kích thước file nhận được không khớp.",
    ErrorCode.HASH_MISMATCH: "File nhận được không toàn vẹn.",
    ErrorCode.DISK_FULL: "Ổ đĩa không còn đủ dung lượng.",
    ErrorCode.PERMISSION_DENIED: "Không có quyền truy cập file hoặc thư mục.",
    ErrorCode.REJECTED_BY_PEER: "Người nhận đã từ chối file.",
    ErrorCode.CANCELLED_BY_PEER: "Người bên kia đã hủy truyền file.",
    ErrorCode.CANCELLED_LOCAL: "Bạn đã hủy truyền file.",
    ErrorCode.INTERNAL: "Đã xảy ra lỗi. Vui lòng thử lại.",
}


def user_message(code: ErrorCode) -> str:
    """Return the central translation; never display unknown peer text as a code."""
    return USER_MESSAGES.get(code, USER_MESSAGES[ErrorCode.INTERNAL])


class LanLinkError(Exception):
    """Business failure with a stable code and a password-free message."""
    def __init__(self, code: ErrorCode, message: str | None = None):
        self.code = ErrorCode(code)
        self.message = message or user_message(self.code)
        super().__init__(self.message)


class ProtocolError(LanLinkError):
    """Fatal malformed frame; do not resynchronise the stream."""


class TransferError(LanLinkError):
    """Expected transfer failure including disk and source-file errors."""
