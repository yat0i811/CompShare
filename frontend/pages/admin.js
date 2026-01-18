// バイトをMBに変換するヘルパー関数
// const bytesToMB = (bytes) => (bytes / 1024 / 1024).toFixed(2); // ドロップダウン形式では直接MB変換は不要かも
// MBをバイトに変換するヘルパー関数
// const mbToBytes = (mb) => parseInt(parseFloat(mb) * 1024 * 1024); // 同上

import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/router';
import useAuth from '../hooks/useAuth';
import { BASE_URL, isTokenExpired } from '../utils/constants';
import styled from 'styled-components';

const StyledAdminContainer = styled.div`
    padding: 20px;
    font-family: 'Arial', sans-serif;
    background-color: #f4f7f6;
    min-height: 100vh;

    h1 {
        color: #2c3e50;
        border-bottom: 2px solid #bdc3c7;
        padding-bottom: 15px;
        margin-bottom: 30px;
        text-align: center;
    }

    h2 {
        color: #34495e;
        margin-top: 25px;
        margin-bottom: 20px;
        border-bottom: 1px solid #ecf0f1;
        padding-bottom: 8px;
    }
`;

const ErrorMessage = styled.p`
    color: #e74c3c;
    background-color: #fdeded;
    border: 1px solid #e74c3c;
    padding: 10px;
    margin-bottom: 20px;
    border-radius: 5px;
`;

const UserSection = styled.section`
    margin-bottom: 30px;
    background-color: #fff;
    padding: 20px;
    border-radius: 8px;
    box-shadow: 0 2px 5px rgba(0, 0, 0, 0.05);
`;

const UserGrid = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); /* レスポンシブなグリッド */
    gap: 20px; /* グリッド間のスペース */
`;

const UserCard = styled.div`
    border: 1px solid #ddd;
    border-radius: 8px;
    padding: 15px;
    background-color: #fff;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
    display: flex;
    flex-direction: column; /* 縦方向に要素を配置 */
    justify-content: space-between; /* 要素間にスペース */
`;

const ButtonContainer = styled.div`
    display: flex;
    gap: 10px; /* ボタン間のスペース */
    margin-top: 15px; /* カード上部とのスペース */
`;

// ユーザー情報表示用のスタイル
const UserInfo = styled.div`
    display: flex;
    flex-direction: column;
    gap: 5px; /* 情報間のスペース */
    margin-bottom: 10px; /* ボタンとのスペース */
`;

// 承認状態のテキストスタイル
const StatusText = styled.span`
    color: ${props => props.isApproved ? '#28a745' : '#dc3545'}; /* 承認済み: 緑, 未承認: 赤 */
    font-weight: bold;
`;

// 権限のテキストスタイル
const RoleText = styled.span`
    color: ${props => props.isAdmin ? '#007bff' : '#6c757d'}; /* 管理者: 青, 一般: グレー */
    font-weight: bold;
`;

const BaseButton = styled.button`
    padding: 8px 15px;
    border: none;
    border-radius: 5px;
    cursor: pointer;
    font-size: 0.9em;
    transition: background-color 0.2s ease;

    &:hover {
        opacity: 0.9;
    }
`;

const ApproveButton = styled(BaseButton)`
    background-color: #2ecc71; /* 緑系 */
    color: white;

    &:hover {
        background-color: #27ae60;
    }
`;

const RejectButton = styled(BaseButton)`
    background-color: #e74c3c; /* 赤系 */
    color: white;

    &:hover {
        background-color: #c0392b;
    }
`;

const RemoveButton = styled(BaseButton)`
    background-color: #f39c12; /* オレンジ系 */
    color: white;

     &:hover {
        background-color: #e67e22;
    }
`;

// 容量選択肢
const capacityOptions = [
    { label: "100MB", value: 100 * 1024 * 1024 },
    { label: "1GB", value: 1 * 1000 * 1024 * 1024 },
    { label: "10GB", value: 10 * 1000 * 1024 * 1024 },
    { label: "100GB", value: 100 * 1000 * 1024 * 1024 },
];

// 容量変更関連のスタイル
const CapacityControl = styled.div`
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 10px;

    select { // input から select に変更
        flex-grow: 1;
        padding: 8px; // 少しパディング調整
        border: 1px solid #ccc;
        border-radius: 4px;
        // width: 80px; // width指定を削除し、flex-growで調整
    }

    button {
        padding: 5px 10px;
        background-color: #3498db; /* 青系 */
        color: white;
        border: none;
        border-radius: 4px;
        cursor: pointer;
        font-size: 0.8em;

        &:hover {
            background-color: #2980b9;
        }

        &:disabled {
            background-color: #ccc;
            cursor: not-allowed;
        }
    }
`;

const AdminPage = () => {
    const router = useRouter();
    const { token, isAdmin, userInfo } = useAuth();
    const [users, setUsers] = useState([]);
    const [pendingUsers, setPendingUsers] = useState([]);
    const [videos, setVideos] = useState([]); // 動画一覧
    const [cleanupFiles, setCleanupFiles] = useState([]); // クリーンアップ対象ファイル一覧
    const [isScanning, setIsScanning] = useState(false); // スキャン中かどうか
    const [isCleaning, setIsCleaning] = useState(false); // クリーンアップ実行中かどうか
    const [error, setError] = useState('');
    const [isLoading, setIsLoading] = useState(true);
    const [updatingUser, setUpdatingUser] = useState(null); // 容量更新中のユーザー名

    useEffect(() => {
        if (token === null || isAdmin === undefined) {
             return;
        }

        // デバッグ用: 管理者情報をコンソールに出力
        if (userInfo) {
            console.log("Current admin user info:", userInfo);
        }

        setIsLoading(false);

        if (!token) {
            router.push('/login');
            return;
        }

        if (isTokenExpired(token)) {
            alert("セッションが切れました。再ログインしてください。");
            localStorage.removeItem("authToken");
            router.push('/login');
            return;
        }

        if (isAdmin) {
             fetchAdminData(token);
        } else {
            setError('管理者権限がありません。');
        }
    }, [token, isAdmin, router, userInfo]);

    const fetchAdminData = async (currentToken) => {
        if (!currentToken) {
            return;
        }
        setError('');

        const formatError = (errorData) => {
            if (Array.isArray(errorData.detail)) {
                return errorData.detail.map(err => `${err.loc.join('.')}: ${err.msg}`).join(', ');
            } else if (errorData.detail) {
                return errorData.detail;
            } else {
                return '不明なエラーが発生しました。';
            }
        };

        try {
            const usersRes = await fetch(`${BASE_URL}/admin/users`, {
                headers: { 'Authorization': `Bearer ${currentToken}` }
            });

            if (usersRes.ok) {
                const usersData = await usersRes.json();
                // ユーザーデータに容量入力用の状態を追加
                setUsers(usersData.map(user => ({
                    ...user,
                    // capacityInput: bytesToMB(user.upload_capacity_bytes) // 初期値をMBで設定 -> バイト値を直接保持
                    selectedCapacity: user.upload_capacity_bytes || capacityOptions[0].value // デフォルトまたは100MB
                })));
            } else {
                const errorData = await usersRes.json().catch(() => ({ detail: 'ユーザー一覧の取得に失敗しました。' }));
                setError(formatError(errorData));
            }

            const pendingUsersRes = await fetch(`${BASE_URL}/admin/pending_users`, {
                headers: { 'Authorization': `Bearer ${currentToken}` }
            });

            if (pendingUsersRes.ok) {
                const pendingUsersData = await pendingUsersRes.json();
                setPendingUsers(pendingUsersData);
            } else {
                const errorData = await pendingUsersRes.json().catch(() => ({ detail: '未承認ユーザー一覧の取得に失敗しました。' }));
                setError(formatError(errorData));
            }

            // 動画一覧の取得
            const videosRes = await fetch(`${BASE_URL}/admin/videos`, {
                headers: { 'Authorization': `Bearer ${currentToken}` }
            });

            if (videosRes.ok) {
                const videosData = await videosRes.json();
                setVideos(videosData);
            } else {
                console.error("Failed to fetch videos");
                // 動画取得失敗は致命的エラーにしない（ユーザー管理はできるため）
            }
        } catch (e) {
            setError('管理者データの取得中にエラーが発生しました。');
        }
    };

    const handleScanCleanup = async () => {
        if (!token) return;
        setIsScanning(true);
        setCleanupFiles([]);
        setError('');
        try {
            const res = await fetch(`${BASE_URL}/admin/cleanup/scan`, {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                const data = await res.json();
                setCleanupFiles(data.files || []);
                if (data.count === 0) {
                    alert("削除対象のファイルは見つかりませんでした。");
                }
            } else {
                const data = await res.json();
                setError(data.detail || "スキャン中にエラーが発生しました。");
            }
        } catch (e) {
            setError("スキャン処理中にエラーが発生しました。");
        } finally {
            setIsScanning(false);
        }
    };

    const handleExecuteCleanup = async () => {
        if (!token) return;
        if (!window.confirm("表示されているファイルを削除しますか？この操作は取り消せません。")) return;
        
        setIsCleaning(true);
        setError('');
        try {
            const res = await fetch(`${BASE_URL}/admin/cleanup/execute`, {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                const data = await res.json();
                alert(`${data.deleted_files.length} 個のファイルを削除しました。`);
                setCleanupFiles([]); // リストをクリア
            } else {
                const data = await res.json();
                setError(data.detail || "クリーンアップ実行中にエラーが発生しました。");
            }
        } catch (e) {
            setError("クリーンアップ実行処理中にエラーが発生しました。");
        } finally {
            setIsCleaning(false);
        }
    };

    const handleApprove = async (username) => {
        if (!token) return;
        setError('');
        try {
            const res = await fetch(`${BASE_URL}/admin/users/${username}/approve`, {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                alert(`${username}を承認しました`);
                fetchAdminData(token);
            } else {
                const errorData = await res.json().catch(() => ({ detail: '不明なエラー' }));
                alert(`承認エラー: ${formatError(errorData)}`);
                setError(formatError(errorData));
            }
        } catch (e) {
            alert('承認処理中にエラーが発生しました');
            setError('承認処理中にエラーが発生しました。');
        }
    };

    const handleReject = async (username) => {
        if (!token) return;
        setError('');
        try {
            const res = await fetch(`${BASE_URL}/admin/users/${username}/reject`, {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                alert(`${username}を拒否しました`);
                fetchAdminData(token);
            } else {
                const errorData = await res.json().catch(() => ({ detail: '不明なエラー' }));
                alert(`拒否エラー: ${formatError(errorData)}`);
                setError(formatError(errorData));
            }
        } catch (e) {
            alert('拒否処理中にエラーが発生しました');
            setError('拒否処理中にエラーが発生しました。');
        }
    };

    const handleRemove = async (username) => {
        if (!token) return;
        setError('');
        try {
            const res = await fetch(`${BASE_URL}/admin/users/${username}`, {
                method: 'DELETE',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                alert(`${username}の登録を取り消しました`);
                fetchAdminData(token);
            } else {
                const errorData = await res.json().catch(() => ({ detail: '不明なエラー' }));
                alert(`登録取り消しエラー: ${formatError(errorData)}`);
                setError(formatError(errorData));
            }
        } catch (e) {
            alert('登録取り消し処理中にエラーが発生しました');
            setError('登録取り消し処理中にエラーが発生しました。');
        }
    };

    const handleDeleteVideo = async (videoId) => {
        if (!token) return;
        if (!window.confirm("この動画を削除しますか？この操作は取り消せません。")) return;
        
        setError('');
        try {
            const res = await fetch(`${BASE_URL}/admin/videos/${videoId}`, {
                method: 'DELETE',
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (res.ok) {
                alert(`動画ID: ${videoId} を削除しました`);
                fetchAdminData(token);
            } else {
                const errorData = await res.json().catch(() => ({ detail: '不明なエラー' }));
                alert(`動画削除エラー: ${errorData.detail}`);
                setError(errorData.detail);
            }
        } catch (e) {
            alert('動画削除処理中にエラーが発生しました');
            console.error(e);
        }
    };

    const handleUpdateCapacity = async (user) => {
        if (!user || user.selectedCapacity === undefined) {
            setError('更新する容量が選択されていません。');
            return;
        }
        setUpdatingUser(user.username);
        setError('');
        const newCapacityInBytes = parseInt(user.selectedCapacity, 10);

        if (isNaN(newCapacityInBytes)) {
            setError('無効な容量値です。');
            setUpdatingUser(null);
            return;
        }

        try {
            const res = await fetch(`${BASE_URL}/admin/users/${user.username}/capacity`, {
                method: 'PUT',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ capacity_bytes: newCapacityInBytes })
            });

            if (res.ok) {
                alert(`ユーザー ${user.username} の容量が正常に更新されました。`);
                fetchAdminData(token); // データを再取得してUIを更新
            } else {
                const errorData = await res.json().catch(() => ({ detail: '容量の更新に失敗しました。' }));
                // setError(formatError(errorData)); // formatErrorがこのスコープにない場合がある
                setError(errorData.detail || '容量の更新に失敗しました。');
            }
        } catch (e) {
            console.error("Capacity update error:", e);
            setError('容量更新中にエラーが発生しました。');
        } finally {
            setUpdatingUser(null);
        }
    };

    // この関数は select の onChange で直接 selectedCapacity を更新するため、
    // 以前の handleCapacityInputChange とは役割が変わります。
    const handleCapacitySelectionChange = (username, selectedValue) => {
        setUsers(prevUsers =>
            prevUsers.map(u =>
                u.username === username ? { ...u, selectedCapacity: parseInt(selectedValue, 10) } : u
            )
        );
    };

    if (isLoading) {
        return (
            <div>
                <h1>管理者ページ</h1>
                <p>読み込み中...</p>
            </div>
        );
    }

    if (!token || !isAdmin) {
        return (
            <div>
                <h1>管理者ページ</h1>
                <p>{error || '管理者権限がありません。'}</p>
            </div>
        );
    }

    return (
        <StyledAdminContainer>
            <h1>管理者ページ</h1>
            {error && <ErrorMessage>エラー: {error}</ErrorMessage>}

            <UserSection>
                <h2>全ユーザー</h2>
                <UserGrid>
                    {users.map(user => {
                        // 現在ログイン中の管理者自身かどうかを判定
                        const isCurrentUser = userInfo && user.username === userInfo.username;
                        return (
                            <UserCard key={user.id}>
                                <UserInfo>
                                    <div>
                                        <strong>ユーザー名:</strong> {user.username}
                                        {isCurrentUser && <span style={{color: '#e74c3c', fontWeight: 'bold', marginLeft: '8px'}}>(あなた)</span>}
                                    </div>
                                    <div>
                                        <strong>承認状態:</strong> <StatusText isApproved={user.is_approved}>{user.is_approved ? '承認済み' : '未承認'}</StatusText>
                                    </div>
                                    <div>
                                         <strong>権限:</strong> <RoleText isAdmin={user.is_admin}>{user.is_admin ? '管理者' : '一般'}</RoleText>
                                    </div>
                                    <div>
                                         <strong>アップロード容量:</strong> {user.upload_capacity_bytes ? `${(user.upload_capacity_bytes / (1000*1024*1024)).toFixed(2)} GB` : '未設定'}
                                    </div>
                                </UserInfo>
                                {/* 管理者も含めて全員の容量変更を可能にする */}
                                <CapacityControl>
                                    <select
                                        value={user.selectedCapacity}
                                        onChange={(e) => handleCapacitySelectionChange(user.username, e.target.value)}
                                        disabled={updatingUser === user.username}
                                    >
                                        {capacityOptions.map(option => (
                                            <option key={option.value} value={option.value}>
                                                {option.label}
                                            </option>
                                        ))}
                                    </select>
                                    <button
                                        onClick={() => handleUpdateCapacity(user)}
                                        disabled={updatingUser === user.username}
                                    >
                                        {updatingUser === user.username ? '更新中...' : '更新'}
                                    </button>
                                </CapacityControl>
                                {/* 管理者は承認・拒否・削除の対象外 */}
                                {!user.is_admin && (
                                    <ButtonContainer>
                                        {!user.is_approved && (
                                            <ApproveButton onClick={() => handleApprove(user.username)}>承認</ApproveButton>
                                        )}
                                        <RejectButton onClick={() => handleReject(user.username)}>拒否</RejectButton>
                                         <RemoveButton onClick={() => handleRemove(user.username)}>削除</RemoveButton>
                                    </ButtonContainer>
                                )}
                            </UserCard>
                        );
                    })}
                </UserGrid>
            </UserSection>

            <UserSection>
                <h2>未承認ユーザー</h2>
                {pendingUsers.length === 0 ? (
                    <p>未承認のユーザーはいません。</p>
                ) : (
                    <ul>
                        {pendingUsers.map(username => (
                            <li key={username}>
                                {username}
                                <ButtonContainer>
                                    <ApproveButton onClick={() => handleApprove(username)}>承認</ApproveButton>
                                    <RejectButton onClick={() => handleReject(username)}>拒否</RejectButton>
                                </ButtonContainer>
                            </li>
                        ))}
                    </ul>
                )}
            </UserSection>

            <UserSection>
                <h2>動画管理</h2>
                {videos.length === 0 ? (
                    <p>共有されている動画はありません。</p>
                ) : (
                    <UserGrid>
                        {videos.map(video => (
                            <UserCard key={video.id}>
                                <UserInfo>
                                    <div><strong>ID:</strong> {video.id}</div>
                                    <div style={{wordBreak: "break-all"}}><strong>元ファイル:</strong> {video.original_filename}</div>
                                    <div style={{wordBreak: "break-all"}}><strong>圧縮ファイル:</strong> {video.compressed_filename}</div>
                                    <div><strong>所有者:</strong> {video.username}</div>
                                    <div><strong>作成日:</strong> {new Date(video.created_at).toLocaleString()}</div>
                                    <div><strong>期限:</strong> {new Date(video.expiry_date).toLocaleString()}</div>
                                </UserInfo>
                                <ButtonContainer>
                                    <RemoveButton onClick={() => handleDeleteVideo(video.id)}>削除</RemoveButton>
                                    <a href={`${BASE_URL.replace('/api', '')}/share/${video.share_token}`} target="_blank" rel="noopener noreferrer" style={{
                                        padding: '8px 15px',
                                        backgroundColor: '#3498db',
                                        color: 'white',
                                        textDecoration: 'none',
                                        borderRadius: '5px',
                                        fontSize: '0.9em'
                                    }}>確認</a>
                                </ButtonContainer>
                            </UserCard>
                        ))}
                    </UserGrid>
                )}
            </UserSection>

            <UserSection>
                <h2>未共有ファイルのクリーンアップ</h2>
                <p>共有リンクが作成されず、作成から3時間以上経過した圧縮ファイルを検索・削除します。</p>
                <ButtonContainer style={{ marginBottom: "20px" }}>
                    <BaseButton 
                        style={{ backgroundColor: "#3498db", color: "white" }} 
                        onClick={handleScanCleanup}
                        disabled={isScanning || isCleaning}
                    >
                        {isScanning ? "スキャン中..." : "スキャン開始"}
                    </BaseButton>
                    {cleanupFiles.length > 0 && (
                        <RemoveButton 
                            onClick={handleExecuteCleanup}
                            disabled={isScanning || isCleaning}
                        >
                           {isCleaning ? "削除中..." : "これらを削除する"}
                        </RemoveButton>
                    )}
                </ButtonContainer>

                {cleanupFiles.length > 0 && (
                    <div>
                        <h3>検出されたファイル ({cleanupFiles.length}件)</h3>
                        <ul style={{ maxHeight: "300px", overflowY: "auto", border: "1px solid #ddd", padding: "10px", borderRadius: "5px" }}>
                            {cleanupFiles.map((file, index) => (
                                <li key={index} style={{ marginBottom: "5px", fontSize: "0.9em" }}>
                                    <strong>{file.key}</strong> <br/>
                                    <span style={{ color: "#666" }}>
                                        サイズ: {(file.size / 1024 / 1024).toFixed(2)} MB, 
                                        更新日: {new Date(file.last_modified).toLocaleString()}
                                    </span>
                                </li>
                            ))}
                        </ul>
                    </div>
                )}
            </UserSection>
        </StyledAdminContainer>
    );
};

export default AdminPage;