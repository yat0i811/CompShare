import { useState, useEffect } from "react";
import Head from 'next/head';
import useVideoProcessing from '../hooks/useVideoProcessing';
import useAuth from '../hooks/useAuth';
import { IS_LOCALHOST } from '../utils/constants';
import { useRouter } from 'next/router';
import Link from 'next/link';

export default function Home() {
  const router = useRouter();
  const { token, handleLogout, username, setUsername, password, setPassword, handleLogin, userInfo } = useAuth();

  const {
    file, setFile,
    originalVideoUrl, setOriginalVideoUrl,
    originalFileSize, setOriginalFileSize,
    compressedVideoUrl, setCompressedVideoUrl,
    compressedFileName, setCompressedFileName,
    compressedFileSize, setCompressedFileSize,
    progress, setProgress,
    crf, setCrf,
    bitrate, setBitrate,
    resolution, setResolution,
    customWidth, setCustomWidth,
    customHeight, setCustomHeight,
    isUploading,
    isDownloading,
    errorMessage, setErrorMessage,
    handleUpload,
    downloadCompressedVideo,
    formatSize,
    estimateCompressedSize,
    getVideoDimensions,
    useGPU, setUseGPU,
    // 共有機能
    compressedR2Key,
    shareUrl,
    shareExpiry, setShareExpiry,
    isCreatingShare,
    shareMessage,
    createShareLink,
    copyShareUrl,
    resetStates,
  } = useVideoProcessing({ token, handleLogout, userInfo });

  const [userUploadCapacity, setUserUploadCapacity] = useState(null);
  const [loadingCapacity, setLoadingCapacity] = useState(true);

  useEffect(() => {
    if (userInfo) {
      setUserUploadCapacity(userInfo.upload_capacity_bytes);
      setLoadingCapacity(false);
    } else if (token) {
      setLoadingCapacity(true);
    } else {
      setLoadingCapacity(false);
    }
  }, [userInfo, token]);

  const handleFileChange = async (event) => {
    const selectedFile = event.target.files[0];
    if (selectedFile) {
      setFile(selectedFile);
      setCompressedVideoUrl("");
      setCompressedFileName("");
      setCompressedFileSize(0);
      setProgress(0);
      setErrorMessage("");
      
      // 動画の解像度を取得してビットレートのデフォルト値を設定
      if (selectedFile.type.startsWith("video/")) {
        try {
          const { width, height, defaultBitrate } = await getVideoDimensions(selectedFile);
          setBitrate(defaultBitrate);
        } catch (error) {
          console.warn("動画の解像度取得に失敗しました:", error);
          setBitrate(3); // デフォルト値
        }
      }
    }
  };

  const handleCustomResolutionChange = (e, type) => {
    const value = e.target.value;
    if (type === "width") {
      setCustomWidth(value);
    } else {
      setCustomHeight(value);
    }
  };

  if (!token) {
    return (
      <div className="login-container">
        <h2>ログインまたはユーザー登録</h2>
        <input 
          type="text" 
          placeholder="ユーザー名" 
          value={username} 
          onChange={(e) => setUsername(e.target.value)} 
        />
        <input 
          type="password" 
          placeholder="パスワード" 
          value={password} 
          onChange={(e) => setPassword(e.target.value)} 
        />
        <button onClick={handleLogin}>ログイン</button>
        
        <p>アカウントをお持ちでない場合はこちら:</p>
        {typeof window !== 'undefined' && (
          <p>
            <Link href="/register">
              ユーザー登録はこちら
            </Link>
          </p>
        )}
        <style jsx>{`
          .login-container {
            max-width: 400px;
            margin: auto;
            padding: 2rem;
            text-align: center;
          }
          .login-container h2 {
            font-size: 1.5rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }
          .login-container input {
            width: 100%;
            padding: 0.5rem;
            margin-bottom: 1rem;
          }
          .login-container button {
            padding: 0.5rem 1rem;
            background: #0070f3;
            color: white;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            margin-bottom: 1rem;
          }
        `}</style>
      </div>
    );
  }

  return (
    <>
      <Head><title>動画圧縮アプリ</title></Head>
      <div className="container">
        <h1>動画圧縮アプリ</h1>
        {errorMessage && <p className="error">⚠️ {errorMessage}</p>}
        <div className="card">
          <input type="file" accept="video/*" onChange={handleFileChange} />
          {loadingCapacity ? (
            <p className="upload-limit-text">アップロード容量を読み込み中...</p>
          ) : userUploadCapacity !== null ? (
            <p className="upload-limit-text">アップロード可能な最大容量: {formatSize(userUploadCapacity)}</p>
          ) : (
            <p className="upload-limit-text">アップロード容量の取得に失敗しました。</p>
          )}
          <div className="control">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={useGPU}
                onChange={(e) => setUseGPU(e.target.checked)}
              />
              GPUを使用して高速化（推奨）
            </label>
          </div>
          {useGPU ? (
            <div className="control">
              <label>ビットレート（CBR）: {bitrate} Mbps</label>
              <input 
                type="range" 
                min="1" 
                max="20" 
                step="0.5"
                value={bitrate} 
                onChange={(e) => setBitrate(parseFloat(e.target.value))} 
              />
              <p className="hint">ビットレートが低いほどファイルサイズが小さくなりますが、画質も低下します。</p>
            </div>
          ) : (
            <div className="control">
              <label>画質（CRF）: {crf}</label>
              <input 
                type="range" 
                min="18" 
                max="32" 
                value={crf} 
                onChange={(e) => setCrf(parseInt(e.target.value, 10))} 
              />
              <p className="hint">CRF値が高いほどファイルサイズが小さくなりますが、画質も低下します。</p>
            </div>
          )}
          {file && !useGPU && (
            <p className="hint">
              推定圧縮後サイズ: {formatSize(estimateCompressedSize(file.size, crf))}
            </p>
          )}
          <div className="control">
            <label>解像度:</label>
            <select value={resolution} onChange={(e) => setResolution(e.target.value)}>
              <option value="source">元の解像度を維持</option>
              <option value="4320p">4320p（8K）</option>
              <option value="2160p">2160p（4K）</option>
              <option value="1440p">1440p（WQHD）</option>
              <option value="1080p">1080p</option>
              <option value="720p">720p</option>
              <option value="480p">480p</option>
              <option value="360p">360p</option>
              <option value="custom">カスタム指定</option>
            </select>
          </div>
          {resolution === "custom" && (
            <div className="control">
              <div className="custom-resolution-inputs">
                <input
                  type="number"
                  placeholder="幅"
                  value={customWidth}
                  onChange={(e) => setCustomWidth(e.target.value)}
                />
                <span>×</span>
                <input
                  type="number"
                  placeholder="高さ"
                  value={customHeight}
                  onChange={(e) => setCustomHeight(e.target.value)}
                />
              </div>
            </div>
          )}
          <button onClick={handleUpload} disabled={!file || isUploading}>
            アップロードして圧縮
          </button>
          {isUploading && (
            <div className="progress-bar-container">
              <div className="progress-bar" style={{ width: `${progress}%` }}></div>
            </div>
          )}
        </div>

        {originalVideoUrl && (
          <div className="card">
            <h2>元動画 ({formatSize(originalFileSize)})</h2>
            <video src={originalVideoUrl} controls width="100%"></video>
          </div>
        )}

        {compressedVideoUrl && (
          <div className="card">
            <h3>圧縮完了</h3>
            <h2>圧縮後動画 ({formatSize(compressedFileSize)})</h2>
            <video src={compressedVideoUrl} controls width="100%"></video>
            <div className="video-actions">
              <button onClick={downloadCompressedVideo} disabled={isDownloading}>
                {isDownloading ? "ダウンロード準備中..." : "ダウンロード"}
              </button>
              {isDownloading && (
                <p className="download-note">
                  ダウンロードリンクを生成中...
                </p>
              )}
            </div>
          </div>
        )}

        {compressedVideoUrl && compressedR2Key && (
          <div className="card">
            <h3>共有機能</h3>
            <div className="share-controls">
              <div className="control">
                <label>有効期限:</label>
                <select value={shareExpiry} onChange={(e) => setShareExpiry(parseInt(e.target.value))}>
                  <option value={1}>1日</option>
                  <option value={3}>3日</option>
                  <option value={7}>7日</option>
                </select>
              </div>
              <button onClick={createShareLink} disabled={isCreatingShare}>
                {isCreatingShare ? "共有リンク作成中..." : "共有リンクを作成"}
              </button>
            </div>
            
            {shareUrl && (
              <div className="share-result">
                <h4>共有URL:</h4>
                <div className="share-url-container">
                  <input 
                    type="text" 
                    value={shareUrl} 
                    readOnly 
                    className="share-url-input"
                  />
                  <button onClick={copyShareUrl} className="copy-button">
                    コピー
                  </button>
                </div>
                <p className="share-note">
                  この共有URLを使用すると、ログインなしで動画をダウンロードできます。
                  有効期限: {shareExpiry}日
                </p>
              </div>
            )}
            
            {shareMessage && (
              <div className={`share-message ${shareMessage.includes('エラー') ? 'error' : 'success'}`}>
                {shareMessage}
              </div>
            )}
          </div>
        )}
      </div>

      <style jsx>{`
        .container {
          max-width: 800px;
          margin: 20px auto;
          padding: 30px;
          border: 1px solid #ccc;
          border-radius: 8px;
          font-family: sans-serif;
        }
        h1 {
          text-align: center;
          margin-bottom: 40px;
          color: #333;
        }
        .error {
            color: red;
            text-align: center;
        }
        .card {
          border: 1px solid #eee;
          padding: 25px;
          margin-bottom: 25px;
          border-radius: 8px;
          text-align: left;
          background-color: #fff;
          box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .control {
          margin-bottom: 20px;
        }
        .control label {
          display: block;
          margin-bottom: 10px;
          font-weight: bold;
          color: #555;
          font-size: 1rem;
        }
        .hint {
          font-size: 0.9em;
          color: #555;
          margin-top: -5px;
          margin-bottom: 10px;
        }
        input[type="file"],
        select,
        input[type="number"] {
          display: block;
          width: calc(100% - 24px);
          padding: 12px;
          margin-bottom: 15px;
          border: 1px solid #ccc;
          border-radius: 4px;
          font-size: 1rem;
          box-sizing: border-box;
        }
        button {
          display: inline-block;
          padding: 12px 20px;
          background-color: #0070f3;
          color: white;
          border: none;
          border-radius: 4px;
          cursor: pointer;
          margin-top: 20px;
          font-size: 1.1rem;
          transition: background-color 0.3s ease;
          width: 100%;
        }
        button:hover {
          background-color: #005bb5;
        }
        button:disabled {
          background-color: #ccc;
          cursor: not-allowed;
        }
        .progress-bar-container {
          width: 100%;
          height: 20px;
          background-color: #e0e0e0;
          border-radius: 10px;
          margin-top: 15px;
          overflow: hidden;
        }
        .progress-bar {
          height: 100%;
          background-color: #76c7c0;
          text-align: center;
          line-height: 20px;
          color: white;
          transition: width 0.5s ease;
        }
        video {
            display: block;
            margin-top: 10px;
        }
        .custom-resolution-inputs {
          display: flex;
          align-items: center;
          gap: 10px;
        }
        .custom-resolution-inputs input[type="number"] {
          width: calc(50% - 15px);
          display: inline-block;
          margin-bottom: 0;
        }
        .custom-resolution-inputs span {
          font-size: 1.1rem;
          font-weight: bold;
        }
        .upload-limit-text {
          font-size: 0.9em;
          color: #555;
          margin-bottom: 10px;
        }
        .checkbox-label {
          display: flex;
          align-items: center;
          gap: 10px;
        }
        
        /* 共有機能のスタイル */
        .share-controls {
          display: flex;
          flex-direction: column;
          gap: 15px;
          margin-bottom: 20px;
        }
        
        .share-controls .control {
          display: flex;
          align-items: center;
          gap: 10px;
        }
        
        .share-controls .control label {
          margin-bottom: 0;
          min-width: 80px;
        }
        
        .share-controls .control select {
          width: 120px;
          margin-bottom: 0;
        }
        
        .share-result {
          margin-top: 20px;
          padding: 15px;
          background-color: #f8f9fa;
          border-radius: 8px;
          border: 1px solid #dee2e6;
        }
        
        .share-result h4 {
          margin-top: 0;
          margin-bottom: 10px;
          color: #333;
        }
        
        .share-url-container {
          display: flex;
          gap: 10px;
          margin-bottom: 10px;
        }
        
        .share-url-input {
          flex: 1;
          padding: 8px;
          border: 1px solid #ccc;
          border-radius: 4px;
          font-size: 0.9rem;
          font-family: monospace;
          background-color: #f8f9fa;
        }
        
        .copy-button {
          padding: 8px 16px;
          background-color: #6c757d;
          color: white;
          border: none;
          border-radius: 4px;
          cursor: pointer;
          font-size: 0.9rem;
          white-space: nowrap;
        }
        
        .copy-button:hover {
          background-color: #5a6268;
        }
        
        .share-note {
          font-size: 0.9rem;
          color: #666;
          margin: 0;
          line-height: 1.4;
        }
        
        .share-message {
          margin-top: 15px;
          padding: 10px;
          border-radius: 4px;
          font-size: 0.9rem;
        }
        
        .share-message.success {
          background-color: #d4edda;
          color: #155724;
          border: 1px solid #c3e6cb;
        }
        
        .share-message.error {
          background-color: #f8d7da;
          color: #721c24;
          border: 1px solid #f5c6cb;
        }
        
        .video-actions {
          display: flex;
          flex-direction: column;
          gap: 10px;
          margin-top: 15px;
        }
        
        .download-note {
          font-size: 0.9rem;
          color: #0070f3;
          margin: 0;
          text-align: center;
          font-weight: 500;
        }
      `}</style>
    </>
  );
}
