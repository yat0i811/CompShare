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
    background-color: var(--panel-alt);
    color: var(--text);
    min-height: 100vh;

    h1 {
        color: var(--text);
        border-bottom: 2px solid var(--panel-border);
        padding-bottom: 15px;
        margin-bottom: 30px;
        text-align: center;
    }

    h2 {
        color: var(--text);
        margin-top: 25px;
        margin-bottom: 20px;
        border-bottom: 1px solid var(--panel-border);
        padding-bottom: 8px;
    }
`;

const ErrorMessage = styled.p`
    color: var(--ng);
    background-color: color-mix(in srgb, var(--ng) 10%, var(--panel));
    border: 1px solid var(--ng);
    padding: 10px;
    margin-bottom: 20px;
    border-radius: 5px;
`;

const UserSection = styled.section`
    margin-bottom: 30px;
    background-color: var(--panel);
    padding: 20px;
    border-radius: 8px;
    box-shadow: var(--shadow);
`;

const UserGrid = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); /* レスポンシブなグリッド */
    gap: 20px; /* グリッド間のスペース */
`;

const UserCard = styled.div`
    border: 1px solid var(--panel-border);
    border-radius: 8px;
    padding: 15px;
    background-color: var(--panel);
    box-shadow: var(--shadow);
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
    color: ${props => props.isApproved ? 'var(--ok)' : 'var(--ng)'}; /* 承認済み: 緑, 未承認: 赤 */
    font-weight: bold;
`;

// 権限のテキストスタイル
const RoleText = styled.span`
    color: ${props => props.isAdmin ? 'var(--accent)' : 'var(--inconclusive)'}; /* 管理者: 青, 一般: グレー */
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
    background-color: var(--ok); /* 緑系 */
    color: var(--accent-contrast);

    &:hover {
        filter: brightness(0.88);
    }
`;

const RejectButton = styled(BaseButton)`
    background-color: var(--ng); /* 赤系 */
    color: var(--accent-contrast);

    &:hover {
        filter: brightness(0.88);
    }
`;

const RemoveButton = styled(BaseButton)`
    background-color: var(--warn); /* オレンジ系 */
    color: var(--warn-contrast);

     &:hover {
        filter: brightness(0.88);
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
        border: 1px solid var(--input-border);
        border-radius: 4px;
        background-color: var(--input-bg);
        color: var(--text);
        // width: 80px; // width指定を削除し、flex-growで調整
    }

    button {
        padding: 5px 10px;
        background-color: var(--accent); /* 青系 */
        color: var(--accent-contrast);
        border: none;
        border-radius: 4px;
        cursor: pointer;
        font-size: 0.8em;

        &:hover {
            background-color: var(--accent-hover);
        }

        &:disabled {
            background-color: var(--panel-border);
            cursor: not-allowed;
        }
    }
`;

// R2ストレージ使用量の判定バッジ。
// 状態色を文字色に使うとライトテーマで AA を満たさない（--ok 3.13:1 / --warn 1.63:1、実測）。
// 状態は「淡色背景 + 状態色のボーダー + --text の文字」で表す。
// この配色は上部の .share-message.success/.error と同じ color-mix パターン。
// styled-components v6 では素の props をDOM要素に転送しようとして警告が出るため、
// カスタム props はトランジェント props（$color）にする。
const StatusBadge = styled.span`
    display: inline-block;
    padding: 4px 12px;
    border-radius: 12px;
    font-weight: bold;
    color: var(--text);
    background-color: color-mix(in srgb, ${props => props.$color} 15%, var(--panel));
    border: 1px solid color-mix(in srgb, ${props => props.$color} 45%, transparent);
`;

// 使用率バー
const UsageBarTrack = styled.div`
    height: 10px;
    border-radius: 5px;
    overflow: hidden;
    background: var(--panel-alt);
    border: 1px solid var(--panel-border);
    margin-top: 8px;
`;

const UsageBarFill = styled.div`
    height: 100%;
    background: ${props => props.$color};
    width: ${props => Math.min(props.$ratio * 100, 100)}%; /* 100% 超は満杯止まりにする */
`;

// R2使用量の status ("within_free" / "near_limit" / "over_free") に対する
// 日本語ラベルと色トークンの対応。バックエンドは表示文言を持たないため、ここで対応付ける。
const R2_USAGE_STATUS_LABELS = {
    within_free: { label: '無料枠内', color: 'var(--ok)' },
    near_limit: { label: '無料枠に近い', color: 'var(--warn)' },
    over_free: { label: '従量課金が発生する見込み', color: 'var(--ng)' },
};

// R2 の請求単位に合わせて 10 進 GB（1GB = 10^9 バイト）で表示する。
// 同ページ上部のアップロード容量表示は 1000*1024*1024 を使っているが、
// あちらはアプリ独自の表示単位なので今回は変更しない（混同しないようここに注記する）。
const formatR2Gb = (bytes) => `${(bytes / 1e9).toFixed(2)} GB`;

// キャッシュ保持時間の表示。応答の cache_ttl_seconds をそのまま使う。
// 注記に「5 分」とハードコードすると、R2_USAGE_CACHE_TTL_SECONDS を変えた時点で嘘になる。
// 値が取れないときは空文字を返し、呼び出し側で「最大 ○○」ごと省略する。
const formatCacheTtl = (seconds) => {
    if (typeof seconds !== 'number' || !isFinite(seconds) || seconds <= 0) return '';
    if (seconds < 60) return `${seconds} 秒`;
    if (seconds % 60 === 0) return `${seconds / 60} 分`;
    return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
};

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
    // R2ストレージ使用量。ページ全体の error(state) とは分離し、
    // 集計失敗が他の管理機能（ユーザー管理等）を巻き込んでエラー扱いにしないようにする。
    const [r2Usage, setR2Usage] = useState(null);
    const [r2UsageError, setR2UsageError] = useState('');
    const [isLoadingR2Usage, setIsLoadingR2Usage] = useState(false);

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

        // R2使用量は独立したエラー state を持つため、ページ全体の error(state) には触れない。
        // 動画一覧取得の失敗を致命扱いしていない上記と同じ方針。
        fetchR2Usage(currentToken);
    };

    // R2ストレージ使用量を取得する。force=true で ?refresh=true を付け、
    // バックエンドのキャッシュ（既定5分）を無視して再走査させる。
    const fetchR2Usage = async (currentToken, { force = false } = {}) => {
        if (!currentToken) return;
        setIsLoadingR2Usage(true);
        setR2UsageError('');
        try {
            const res = await fetch(`${BASE_URL}/admin/r2/usage${force ? '?refresh=true' : ''}`, {
                headers: { 'Authorization': `Bearer ${currentToken}` }
            });
            if (res.ok) {
                const data = await res.json();
                setR2Usage(data);
            } else {
                const errorData = await res.json().catch(() => ({ detail: 'R2使用量の取得に失敗しました。' }));
                setR2UsageError(errorData.detail || 'R2使用量の取得に失敗しました。');
            }
        } catch (e) {
            setR2UsageError('R2使用量の取得中にエラーが発生しました。');
        } finally {
            setIsLoadingR2Usage(false);
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
                                        {isCurrentUser && <span style={{color: 'var(--ng)', fontWeight: 'bold', marginLeft: '8px'}}>(あなた)</span>}
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
                                    {/* Next.js の共有ページ /share/{token} を開く。
                                        BASE_URL（本番では '/be'）を前置しないこと。
                                        前置するとバックエンドの生HTMLページ /be/share/{token} に飛んでしまう。 */}
                                    <a href={`/share/${video.share_token}`} target="_blank" rel="noopener noreferrer" style={{
                                        padding: '8px 15px',
                                        backgroundColor: 'var(--accent)',
                                        color: 'var(--accent-contrast)',
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
                <h2>R2ストレージ</h2>
                {r2UsageError && <ErrorMessage>R2使用量エラー: {r2UsageError}</ErrorMessage>}
                {!r2Usage && isLoadingR2Usage && <p>読み込み中...</p>}
                {!r2Usage && !isLoadingR2Usage && !r2UsageError && <p>R2使用量を取得できませんでした。</p>}
                {r2Usage && (
                    <div>
                        <UserGrid>
                            <UserCard>
                                <UserInfo>
                                    <div><strong>合計使用量:</strong> {formatR2Gb(r2Usage.total_bytes)}</div>
                                    <div><strong>オブジェクト数:</strong> {r2Usage.object_count} 件</div>
                                    <div>
                                        <strong>無料枠:</strong>{' '}
                                        {r2Usage.free_tier_bytes ? formatR2Gb(r2Usage.free_tier_bytes) : '-'}
                                        {'　'}
                                        <strong>使用率:</strong>{' '}
                                        {typeof r2Usage.usage_ratio === 'number' ? `${(r2Usage.usage_ratio * 100).toFixed(1)}%` : '-'}
                                    </div>
                                    <UsageBarTrack>
                                        <UsageBarFill
                                            $ratio={typeof r2Usage.usage_ratio === 'number' ? r2Usage.usage_ratio : 0}
                                            $color={(R2_USAGE_STATUS_LABELS[r2Usage.status] || {}).color || 'var(--accent)'}
                                        />
                                    </UsageBarTrack>
                                    <div>
                                        <strong>判定:</strong>{' '}
                                        <StatusBadge $color={(R2_USAGE_STATUS_LABELS[r2Usage.status] || {}).color || 'var(--inconclusive)'}>
                                            {(R2_USAGE_STATUS_LABELS[r2Usage.status] || {}).label || r2Usage.status}
                                        </StatusBadge>
                                    </div>
                                    {r2Usage.status === 'over_free' && (
                                        <div>
                                            <strong>概算超過額:</strong> ${r2Usage.estimated_monthly_cost_usd.toFixed(2)} / 月
                                            （${r2Usage.price_per_gb_month_usd} / GB-month）
                                        </div>
                                    )}
                                </UserInfo>
                            </UserCard>

                            {(r2Usage.prefixes || []).map(p => (
                                <UserCard key={p.prefix}>
                                    <UserInfo>
                                        <div><strong>{p.prefix}</strong></div>
                                        <div>{formatR2Gb(p.bytes)}</div>
                                        <div>{p.count} 件</div>
                                    </UserInfo>
                                </UserCard>
                            ))}
                        </UserGrid>

                        <p style={{ color: 'var(--muted)', fontSize: '0.9em', marginTop: '15px' }}>
                            集計時刻: {new Date(r2Usage.collected_at).toLocaleString('ja-JP')}
                            {r2Usage.cached ? '（キャッシュされた結果）' : '（今回取得）'}
                        </p>

                        <ButtonContainer>
                            <BaseButton
                                style={{ backgroundColor: 'var(--accent)', color: 'var(--accent-contrast)' }}
                                onClick={() => fetchR2Usage(token, { force: true })}
                                disabled={isLoadingR2Usage}
                            >
                                {isLoadingR2Usage ? '再取得中...' : '再取得'}
                            </BaseButton>
                        </ButtonContainer>

                        <ul style={{ color: 'var(--muted)', fontSize: '0.85em', marginTop: '15px', paddingLeft: '20px' }}>
                            <li>実際の課金は日次ピーク値を30日で平均した GB-month で計算されるため、ここに出る値は概算です。</li>
                            <li>課金対象はデータ本体とメタデータの合計ですが、ここではデータ本体のみを集計しています。</li>
                            {/* キャッシュ秒数は応答の cache_ttl_seconds から出す。
                                ハードコードすると R2_USAGE_CACHE_TTL_SECONDS を変えた時点で注記が嘘になる。 */}
                            <li>
                                {(() => {
                                    const ttl = formatCacheTtl(r2Usage.cache_ttl_seconds);
                                    return `一覧取得は Cloudflare の Class A オペレーションを消費するため、結果を${ttl ? `最大 ${ttl}` : ''}キャッシュします。`;
                                })()}
                            </li>
                        </ul>
                    </div>
                )}
            </UserSection>

            <UserSection>
                <h2>未共有ファイルのクリーンアップ</h2>
                <p>共有リンクが作成されず、作成から3時間以上経過した圧縮ファイルを検索・削除します。</p>
                <ButtonContainer style={{ marginBottom: "20px" }}>
                    <BaseButton
                        style={{ backgroundColor: "var(--accent)", color: "var(--accent-contrast)" }}
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
                        <ul style={{ maxHeight: "300px", overflowY: "auto", border: "1px solid var(--panel-border)", padding: "10px", borderRadius: "5px" }}>
                            {cleanupFiles.map((file, index) => (
                                <li key={index} style={{ marginBottom: "5px", fontSize: "0.9em" }}>
                                    <strong>{file.key}</strong> <br/>
                                    <span style={{ color: "var(--muted)" }}>
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