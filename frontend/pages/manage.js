import React, { useState, useEffect } from 'react';
import useAuth from '../hooks/useAuth';
import { useRouter } from 'next/router';

const ManagePage = () => {
    const [videos, setVideos] = useState([]);
    const [stats, setStats] = useState({ total_videos: 0, active_videos: 0 });
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [selectedVideo, setSelectedVideo] = useState(null);
    const [showExpiryModal, setShowExpiryModal] = useState(false);
    const [newExpiryDays, setNewExpiryDays] = useState(7);
    const [updatingExpiry, setUpdatingExpiry] = useState(false);
    const [deletingVideo, setDeletingVideo] = useState(null);
    
    const { token } = useAuth();
    const router = useRouter();

    useEffect(() => {
        if (!token) {
            router.push('/');
            return;
        }
        fetchVideos();
        fetchStats();
    }, [token]);

    const fetchVideos = async () => {
        try {
            const response = await fetch(`${process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8001'}/manage/videos`, {
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });
            
            if (!response.ok) {
                throw new Error('動画一覧の取得に失敗しました');
            }
            
            const data = await response.json();
            setVideos(data.videos);
        } catch (err) {
            setError(err.message);
        } finally {
            setLoading(false);
        }
    };

    const fetchStats = async () => {
        try {
            const response = await fetch(`${process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8001'}/manage/stats`, {
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });
            
            if (response.ok) {
                const data = await response.json();
                setStats(data);
            }
        } catch (err) {
            console.error('統計情報の取得に失敗:', err);
        }
    };

    const handlePreview = (video) => {
        window.open(video.share_url, '_blank');
    };

    const handleDownload = async (video) => {
        try {
            const response = await fetch(`${process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8001'}/get-download-url/${video.compressed_filename}`, {
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });
            
            if (!response.ok) {
                throw new Error('ダウンロードURLの取得に失敗しました');
            }
            
            const data = await response.json();
            window.open(data.download_url, '_blank');
        } catch (err) {
            alert('ダウンロードに失敗しました: ' + err.message);
        }
    };

    const handleDelete = async (video) => {
        if (!confirm(`「${video.original_filename}」を削除しますか？\nこの操作は取り消せません。`)) {
            return;
        }

        setDeletingVideo(video.share_token);
        
        try {
            const response = await fetch(`${process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8001'}/manage/delete/${video.share_token}`, {
                method: 'DELETE',
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });
            
            if (!response.ok) {
                throw new Error('動画の削除に失敗しました');
            }
            
            // 成功メッセージを表示
            alert('動画が正常に削除されました');
            
            // 一覧を再取得
            fetchVideos();
            fetchStats();
        } catch (err) {
            alert('削除に失敗しました: ' + err.message);
        } finally {
            setDeletingVideo(null);
        }
    };

    const handleUpdateExpiry = async () => {
        if (!selectedVideo) return;
        
        setUpdatingExpiry(true);
        
        try {
            const response = await fetch(`${process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8001'}/manage/update-expiry/${selectedVideo.share_token}?new_expiry_days=${newExpiryDays}`, {
                method: 'PUT',
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });
            
            if (!response.ok) {
                throw new Error('有効期限の更新に失敗しました');
            }
            
            alert('有効期限が正常に更新されました');
            setShowExpiryModal(false);
            fetchVideos();
        } catch (err) {
            alert('有効期限の更新に失敗しました: ' + err.message);
        } finally {
            setUpdatingExpiry(false);
        }
    };

    const openExpiryModal = (video) => {
        setSelectedVideo(video);
        setNewExpiryDays(7);
        setShowExpiryModal(true);
    };

    const formatDate = (dateString) => {
        const date = new Date(dateString);
        return date.toLocaleString('ja-JP');
    };

    const formatFileSize = (bytes) => {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    };

    if (loading) {
        return <div className="loading">読み込み中...</div>;
    }

    if (error) {
        return <div className="error">エラー: {error}</div>;
    }

    return (
        <div className="manage-page">
            <h1>動画管理ページ</h1>
                
            <div className="stats-container">
                <div className="stat-card">
                    <h3>共有中の動画</h3>
                    <p>{stats.active_videos}</p>
                </div>
            </div>

            {videos.length === 0 ? (
                <div className="no-videos">
                    <p>まだ動画がありません。</p>
                    <p>ホームページで動画をアップロードして圧縮してください。</p>
                </div>
            ) : (
                <div className="videos-grid">
                    {videos.map((video) => (
                        <div key={video.share_token} className={`video-card ${video.is_expired ? 'expired' : ''}`}>
                            <div className="video-header">
                                <h3>{video.original_filename}</h3>
                                {video.is_expired && <span className="expired-badge">期限切れ</span>}
                            </div>
                            
                            <div className="video-info">
                                <p><strong>圧縮ファイル:</strong> {video.compressed_filename}</p>
                                <p><strong>作成日:</strong> {formatDate(video.created_at)}</p>
                                <p><strong>有効期限:</strong> {formatDate(video.expiry_date)}</p>
                                {!video.is_expired && (
                                    <p><strong>残り日数:</strong> {video.remaining_days}日</p>
                                )}
                            </div>

                            <div className="video-actions">
                                <button 
                                    onClick={() => handlePreview(video)}
                                    className="btn btn-preview"
                                >
                                    プレビュー
                                </button>
                                
                                <button 
                                    onClick={() => handleDownload(video)}
                                    className="btn btn-download"
                                >
                                    ダウンロード
                                </button>
                                
                                <button 
                                    onClick={() => openExpiryModal(video)}
                                    className="btn btn-expiry"
                                    disabled={video.is_expired}
                                >
                                    期間変更
                                </button>
                                
                                <button 
                                    onClick={() => handleDelete(video)}
                                    className="btn btn-delete"
                                    disabled={deletingVideo === video.share_token}
                                >
                                    {deletingVideo === video.share_token ? '削除中...' : '削除'}
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            {/* 有効期限変更モーダル */}
            {showExpiryModal && (
                <div className="modal-overlay">
                    <div className="modal">
                        <h3>有効期限の変更</h3>
                        <p>「{selectedVideo?.original_filename}」の有効期限を変更します。</p>
                        
                        <div className="form-group">
                            <label>新しい有効期限（日数）:</label>
                            <select
                                value={newExpiryDays}
                                onChange={(e) => setNewExpiryDays(Number(e.target.value))}
                                className="form-input"
                            >
                                <option value={1}>1日</option>
                                <option value={3}>3日</option>
                                <option value={7}>7日</option>
                            </select>
                        </div>
                        
                        <div className="modal-actions">
                            <button 
                                onClick={() => setShowExpiryModal(false)}
                                className="btn btn-secondary"
                                disabled={updatingExpiry}
                            >
                                キャンセル
                            </button>
                            <button 
                                onClick={handleUpdateExpiry}
                                className="btn btn-primary"
                                disabled={updatingExpiry}
                            >
                                {updatingExpiry ? '更新中...' : '更新'}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            <style jsx>{`
                .manage-page {
                    max-width: 1200px;
                    margin: 0 auto;
                    padding: 20px;
                }

                .manage-page h1 {
                    text-align: center;
                    margin-bottom: 30px;
                    color: #333;
                }

                .stats-container {
                    display: flex;
                    gap: 20px;
                    margin-bottom: 30px;
                    justify-content: center;
                }

                .stat-card {
                    background: #f8f9fa;
                    padding: 20px;
                    border-radius: 8px;
                    text-align: center;
                    min-width: 150px;
                    border: 1px solid #e9ecef;
                }

                .stat-card h3 {
                    margin: 0 0 10px 0;
                    color: #666;
                    font-size: 14px;
                }

                .stat-card p {
                    margin: 0;
                    font-size: 24px;
                    font-weight: bold;
                    color: #333;
                }

                .no-videos {
                    text-align: center;
                    padding: 60px 20px;
                    color: #666;
                }

                .videos-grid {
                    display: grid;
                    grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
                    gap: 20px;
                }

                .video-card {
                    background: white;
                    border: 1px solid #e9ecef;
                    border-radius: 8px;
                    padding: 20px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                }

                .video-card.expired {
                    opacity: 0.7;
                    background: #f8f9fa;
                }

                .video-header {
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 15px;
                }

                .video-header h3 {
                    margin: 0;
                    font-size: 16px;
                    color: #333;
                    word-break: break-all;
                }

                .expired-badge {
                    background: #dc3545;
                    color: white;
                    padding: 4px 8px;
                    border-radius: 4px;
                    font-size: 12px;
                    font-weight: bold;
                }

                .video-info {
                    margin-bottom: 20px;
                }

                .video-info p {
                    margin: 5px 0;
                    font-size: 14px;
                    color: #666;
                }

                .video-info strong {
                    color: #333;
                }

                .video-actions {
                    display: flex;
                    gap: 10px;
                    flex-wrap: wrap;
                }

                .btn {
                    padding: 8px 16px;
                    border: none;
                    border-radius: 4px;
                    cursor: pointer;
                    font-size: 14px;
                    transition: background-color 0.2s;
                }

                .btn:disabled {
                    opacity: 0.6;
                    cursor: not-allowed;
                }

                .btn-preview {
                    background: #007bff;
                    color: white;
                }

                .btn-preview:hover:not(:disabled) {
                    background: #0056b3;
                }

                .btn-download {
                    background: #28a745;
                    color: white;
                }

                .btn-download:hover:not(:disabled) {
                    background: #1e7e34;
                }

                .btn-expiry {
                    background: #ffc107;
                    color: #212529;
                }

                .btn-expiry:hover:not(:disabled) {
                    background: #e0a800;
                }

                .btn-delete {
                    background: #dc3545;
                    color: white;
                }

                .btn-delete:hover:not(:disabled) {
                    background: #c82333;
                }

                .modal-overlay {
                    position: fixed;
                    top: 0;
                    left: 0;
                    right: 0;
                    bottom: 0;
                    background: rgba(0,0,0,0.5);
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    z-index: 1000;
                }

                .modal {
                    background: white;
                    padding: 30px;
                    border-radius: 8px;
                    max-width: 500px;
                    width: 90%;
                }

                .modal h3 {
                    margin: 0 0 20px 0;
                    color: #333;
                }

                .form-group {
                    margin-bottom: 20px;
                }

                .form-group label {
                    display: block;
                    margin-bottom: 5px;
                    color: #333;
                    font-weight: bold;
                }

                .form-input {
                    width: 100%;
                    padding: 10px;
                    border: 1px solid #ddd;
                    border-radius: 4px;
                    font-size: 16px;
                }

                .modal-actions {
                    display: flex;
                    gap: 10px;
                    justify-content: flex-end;
                }

                .btn-secondary {
                    background: #6c757d;
                    color: white;
                }

                .btn-secondary:hover:not(:disabled) {
                    background: #545b62;
                }

                .btn-primary {
                    background: #007bff;
                    color: white;
                }

                .btn-primary:hover:not(:disabled) {
                    background: #0056b3;
                }

                .loading, .error {
                    text-align: center;
                    padding: 60px 20px;
                    font-size: 18px;
                }

                .error {
                    color: #dc3545;
                }

                @media (max-width: 768px) {
                    .videos-grid {
                        grid-template-columns: 1fr;
                    }
                    
                    .stats-container {
                        flex-direction: column;
                        align-items: center;
                    }
                    
                    .video-actions {
                        flex-direction: column;
                    }
                    
                    .btn {
                        width: 100%;
                    }
                }
            `}</style>
        </div>
    );
};

export default ManagePage; 