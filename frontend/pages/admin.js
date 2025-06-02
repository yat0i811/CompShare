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
                    selectedCapacity: user.upload_capacity_bytes || capacityOptions[1].value // デフォルトまたは1GB
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
        } catch (e) {
            setError('管理者データの取得中にエラーが発生しました。');
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
        </StyledAdminContainer>
    );
};

export default AdminPage;