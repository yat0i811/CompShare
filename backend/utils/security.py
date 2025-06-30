import re
import logging
import os
from datetime import datetime
from typing import Optional
from fastapi import Request
import unicodedata

# セキュリティログの設定
security_logger = logging.getLogger('security')
security_logger.setLevel(logging.INFO)

# ログファイルの設定
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'logs')
try:
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
except Exception as e:
    print(f"Warning: Could not create log directory {log_dir}: {e}")
    # フォールバック: カレントディレクトリにlogsフォルダを作成
    log_dir = 'logs'
    if not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)

security_handler = logging.FileHandler(os.path.join(log_dir, 'security.log'), encoding='utf-8')
security_formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
security_handler.setFormatter(security_formatter)
security_logger.addHandler(security_handler)

def sanitize_filename(filename: str) -> str:
    """
    ファイル名をサニタイズして安全なファイル名を返す
    
    Args:
        filename: 元のファイル名
        
    Returns:
        サニタイズされたファイル名
    """
    if not filename:
        return "unnamed_file"
    
    # 危険な文字を除去または置換
    # パス区切り文字を除去
    filename = re.sub(r'[\\/:*?"<>|]', '_', filename)
    
    # 制御文字を除去
    filename = ''.join(char for char in filename if unicodedata.category(char)[0] != 'C')
    
    # 先頭と末尾の空白、ドットを除去
    filename = filename.strip(' .')
    
    # 連続するアンダースコアを単一のアンダースコアに置換
    filename = re.sub(r'_+', '_', filename)
    
    # ファイル名が空になった場合のデフォルト
    if not filename:
        return "unnamed_file"
    
    # ファイル名の長さ制限（255文字）
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        max_name_length = 255 - len(ext)
        filename = name[:max_name_length] + ext
    
    return filename

def validate_filename(filename: str) -> bool:
    """
    ファイル名が安全かどうかを検証する
    
    Args:
        filename: 検証するファイル名
        
    Returns:
        安全な場合はTrue、危険な場合はFalse
    """
    if not filename:
        return False
    
    # 危険なパターンをチェック
    dangerous_patterns = [
        r'\.\.',  # ディレクトリトラバーサル
        r'^\.',   # 隠しファイル
        r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$',  # Windows予約名
        r'[\\/:*?"<>|]',  # 危険な文字
    ]
    
    for pattern in dangerous_patterns:
        if re.search(pattern, filename, re.IGNORECASE):
            return False
    
    return True

def log_security_event(
    event_type: str,
    user: Optional[str] = None,
    ip_address: Optional[str] = None,
    details: Optional[str] = None,
    severity: str = "INFO"
):
    """
    セキュリティイベントをログに記録する
    
    Args:
        event_type: イベントの種類
        user: ユーザー名
        ip_address: IPアドレス
        details: 詳細情報
        severity: 重要度（INFO, WARNING, ERROR, CRITICAL）
    """
    log_message = f"SECURITY_EVENT - Type: {event_type}"
    
    if user:
        log_message += f" | User: {user}"
    
    if ip_address:
        log_message += f" | IP: {ip_address}"
    
    if details:
        log_message += f" | Details: {details}"
    
    if severity == "INFO":
        security_logger.info(log_message)
    elif severity == "WARNING":
        security_logger.warning(log_message)
    elif severity == "ERROR":
        security_logger.error(log_message)
    elif severity == "CRITICAL":
        security_logger.critical(log_message)

def log_file_upload_attempt(
    request: Request,
    user: str,
    filename: str,
    file_size: int,
    success: bool,
    error_message: Optional[str] = None
):
    """
    ファイルアップロード試行をログに記録する
    
    Args:
        request: FastAPIリクエストオブジェクト
        user: ユーザー名
        filename: ファイル名
        file_size: ファイルサイズ
        success: 成功したかどうか
        error_message: エラーメッセージ（失敗時）
    """
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "unknown")
    
    event_type = "FILE_UPLOAD_SUCCESS" if success else "FILE_UPLOAD_FAILURE"
    severity = "INFO" if success else "WARNING"
    
    details = f"Filename: {filename}, Size: {file_size}, User-Agent: {user_agent}"
    if error_message:
        details += f", Error: {error_message}"
    
    log_security_event(
        event_type=event_type,
        user=user,
        ip_address=client_ip,
        details=details,
        severity=severity
    )

def log_security_violation(
    request: Request,
    user: Optional[str],
    violation_type: str,
    details: str
):
    """
    セキュリティ違反をログに記録する
    
    Args:
        request: FastAPIリクエストオブジェクト
        user: ユーザー名（不明な場合はNone）
        violation_type: 違反の種類
        details: 詳細情報
    """
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "unknown")
    
    full_details = f"Violation: {violation_type}, Details: {details}, User-Agent: {user_agent}"
    
    log_security_event(
        event_type="SECURITY_VIOLATION",
        user=user,
        ip_address=client_ip,
        details=full_details,
        severity="WARNING"
    )

def log_authentication_event(
    request: Request,
    username: str,
    success: bool,
    details: Optional[str] = None
):
    """
    認証イベントをログに記録する
    
    Args:
        request: FastAPIリクエストオブジェクト
        username: ユーザー名
        success: 認証成功かどうか
        details: 詳細情報
    """
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "unknown")
    
    event_type = "AUTH_SUCCESS" if success else "AUTH_FAILURE"
    severity = "INFO" if success else "WARNING"
    
    full_details = f"User-Agent: {user_agent}"
    if details:
        full_details += f", Details: {details}"
    
    log_security_event(
        event_type=event_type,
        user=username,
        ip_address=client_ip,
        details=full_details,
        severity=severity
    )

def get_client_ip(request: Request) -> str:
    """
    クライアントのIPアドレスを取得する
    プロキシ環境でも正しいグローバルIPを取得することを試みる
    
    Args:
        request: FastAPIリクエストオブジェクト
        
    Returns:
        クライアントのIPアドレス
    """
    # プロキシヘッダーからIPアドレスを取得（信頼できる順序）
    proxy_headers = [
        'X-Forwarded-For',
        'X-Real-IP',
        'X-Client-IP',
        'CF-Connecting-IP',  # Cloudflare
        'X-Forwarded',
        'Forwarded-For',
        'Forwarded'
    ]
    
    for header in proxy_headers:
        ip = request.headers.get(header)
        if ip:
            # X-Forwarded-Forは複数のIPが含まれる場合があるので最初のものを取得
            if ',' in ip:
                ip = ip.split(',')[0].strip()
            # プライベートIPアドレスでないことを確認
            if not is_private_ip(ip):
                return ip
    
    # プロキシヘッダーがない場合は直接のクライアントIPを使用
    if request.client:
        return request.client.host
    
    return "unknown"

def is_private_ip(ip: str) -> bool:
    """
    IPアドレスがプライベートIPかどうかを判定する
    
    Args:
        ip: IPアドレス
        
    Returns:
        プライベートIPの場合はTrue
    """
    if not ip or ip == "unknown":
        return False
    
    # プライベートIPアドレスの範囲
    private_ranges = [
        ('10.0.0.0', '10.255.255.255'),
        ('172.16.0.0', '172.31.255.255'),
        ('192.168.0.0', '192.168.255.255'),
        ('127.0.0.0', '127.255.255.255'),  # localhost
        ('169.254.0.0', '169.254.255.255'),  # link-local
        ('::1', '::1'),  # IPv6 localhost
        ('fe80::', 'fe80::ffff:ffff:ffff:ffff'),  # IPv6 link-local
    ]
    
    try:
        ip_parts = [int(part) for part in ip.split('.')]
        if len(ip_parts) == 4:  # IPv4
            ip_num = (ip_parts[0] << 24) + (ip_parts[1] << 16) + (ip_parts[2] << 8) + ip_parts[3]
            
            for start_ip, end_ip in private_ranges:
                if start_ip == '::1':  # IPv6の場合はスキップ
                    continue
                start_parts = [int(part) for part in start_ip.split('.')]
                end_parts = [int(part) for part in end_ip.split('.')]
                start_num = (start_parts[0] << 24) + (start_parts[1] << 16) + (start_parts[2] << 8) + start_parts[3]
                end_num = (end_parts[0] << 24) + (end_parts[1] << 16) + (end_parts[2] << 8) + end_parts[3]
                
                if start_num <= ip_num <= end_num:
                    return True
    except (ValueError, IndexError):
        pass
    
    return False 