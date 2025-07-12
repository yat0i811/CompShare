import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/router';
import Link from 'next/link';

const SharePage = () => {
    const router = useRouter();
    const { id } = router.query;
    const [isLoading, setIsLoading] = useState(true);
    const [fileExists, setFileExists] = useState(false);
    const [fileInfo, setFileInfo] = useState(null);

    useEffect(() => {
        if (id) {
            checkFileExists();
        }
    }, [id]);

    const checkFileExists = async () => {
        try {
            // バックエンドAPIを呼び出してファイルの存在確認
            const response = await fetch(`/api/share/${id}`);
            if (response.ok) {
                const data = await response.json();
                setFileInfo(data);
                setFileExists(true);
            } else {
                const errorData = await response.json();
                console.error('ファイル確認エラー:', errorData);
                setFileExists(false);
            }
        } catch (error) {
            console.error('ファイル確認エラー:', error);
            setFileExists(false);
        } finally {
            setIsLoading(false);
        }
    };

    const handleDownload = async () => {
        if (!fileInfo) return;
        
        try {
            const response = await fetch(`/api/download/${id}`);
            if (response.ok) {
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = fileInfo.filename || 'download';
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
                document.body.removeChild(a);
            } else {
                throw new Error('ダウンロードに失敗しました');
            }
        } catch (error) {
            console.error('ダウンロードエラー:', error);
            alert('ダウンロードに失敗しました。ファイルが存在しないか、アクセス権限がありません。');
        }
    };

    if (isLoading) {
        return (
            <div className="error-container">
                <div className="loading-spinner"></div>
                <p>ファイルを確認中...</p>
            </div>
        );
    }

    if (!fileExists) {
        return (
            <div className="error-container">
                <div className="error-icon">📁</div>
                <h1>ファイルが見つかりません</h1>
                <p className="error-message">ダウンロードできるファイルがありません。</p>
                <div className="error-details">
                    <p>以下の理由が考えられます：</p>
                    <ul>
                        <li>共有リンクが見つからない</li>
                        <li>ファイルが削除されている</li>
                        <li>共有URLが無効になっている</li>
                        <li>URLが間違っている</li>
                        <li>ファイルの有効期限が切れている</li>
                    </ul>
                </div>
                <div className="action-buttons">
                    <Link href="/">
                        <button className="home-button">ホームに戻る</button>
                    </Link>
                </div>
            </div>
        );
    }

    return (
        <div className="share-container">
            <div className="file-info">
                <div className="file-icon">📄</div>
                <h1>ファイルのダウンロード</h1>
                <div className="file-details">
                    <p><strong>ファイル名:</strong> {fileInfo.filename}</p>
                    {fileInfo.size && (
                        <p><strong>サイズ:</strong> {formatFileSize(fileInfo.size)}</p>
                    )}
                    {fileInfo.uploaded_at && (
                        <p><strong>アップロード日時:</strong> {new Date(fileInfo.uploaded_at).toLocaleString('ja-JP')}</p>
                    )}
                </div>
                <button className="download-button" onClick={handleDownload}>
                    ダウンロード
                </button>
            </div>
        </div>
    );
};

const formatFileSize = (bytes) => {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
};

export default SharePage; 