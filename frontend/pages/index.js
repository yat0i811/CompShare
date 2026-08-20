import { useState, useEffect } from "react";
import Head from 'next/head';
import useVideoProcessing from '../hooks/useVideoProcessing';
import ProgressPanel from '../components/ProgressPanel';
import useAuth from '../hooks/useAuth';
import { IS_LOCALHOST, isLocalhost } from '../utils/constants';
import { useRouter } from 'next/router';
import Link from 'next/link';

export default function Home() {
  const router = useRouter();
  const { token, handleLogout, username, setUsername, password, setPassword, handleLogin, userInfo, userInfoFetchFailed, refreshUserInfo } = useAuth();

  const {
    file, setFile,
    selectFile,
    originalVideoUrl, setOriginalVideoUrl,
    originalFileSize, setOriginalFileSize,
    compressedVideoUrl, setCompressedVideoUrl,
    compressedFileName, setCompressedFileName,
    compressedFileSize, setCompressedFileSize,
    progress, setProgress,
    stage,
    crf, setCrf,
    resolution, setResolution,
    customWidth, setCustomWidth,
    customHeight, setCustomHeight,
    isUploading,
    isDownloading,
    errorMessage, setErrorMessage,
    handleUpload,
    downloadCompressedVideo,
    formatSize,
    reductionRate,
    estimateCompressedSize,
    estimateCompressedSizeRange,
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
  } = useVideoProcessing({ token, handleLogout, userInfo, refreshUserInfo });

  const [userUploadCapacity, setUserUploadCapacity] = useState(null);
  const [loadingCapacity, setLoadingCapacity] = useState(true);

  useEffect(() => {
    if (userInfo) {
      setUserUploadCapacity(userInfo.upload_capacity_bytes);
      setLoadingCapacity(false);
    } else if (token) {
      // 取得が失敗で確定した場合は「読み込み中」を解除する。
      // ここを解除しないと、アップロードボタンの disabled に loadingCapacity を
      // 加えたことで、取得失敗時にボタンが永久に押せなくなってしまう
      // （押せれば handleUpload 内で /auth/me を再取得して復帰できる）。
      setUserUploadCapacity(null);
      setLoadingCapacity(!userInfoFetchFailed);
    } else {
      setLoadingCapacity(false);
    }
  }, [userInfo, token, userInfoFetchFailed]);



  const handleFileChange = (event) => {
    const selectedFile = event.target.files[0];
    if (selectedFile) {
      // selectFile() が file / originalVideoUrl / originalFileSize をまとめて更新し、
      // 差し替え前の blob URL を revoke する（詳細は useVideoProcessing.js 参照）。
      selectFile(selectedFile);
      // 圧縮結果・共有URL等、前回選択分の残骸を一括リセットする。
      resetStates();
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
            background: var(--accent);
            color: var(--accent-contrast);
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
      <Head><title>CompShare</title></Head>
      <div className="container">
        <h1>動画圧縮共有サービス</h1>
        {errorMessage && <p className="error">{errorMessage}</p>}
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
            {!useGPU && (
              <p className="hint">
                GPUを使わない場合、大きな動画では圧縮に20〜30分かかります。
              </p>
            )}
          </div>
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
          {file && (() => {
            const range = estimateCompressedSizeRange(file.size, crf);
            return (
              <>
                <p className="hint">
                  推定圧縮後サイズ: {range ? `およそ ${formatSize(range.min)} 〜 ${formatSize(range.max)}` : "-"}
                </p>
                <p className="hint">動画の内容（動きの多さ）により変動します。</p>
                {useGPU && (
                  <p className="hint">GPU（NVENC）では、同じ画質指定でもファイルサイズがCPUと異なる場合があります。</p>
                )}
              </>
            );
          })()}
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
          {/* 容量取得中は押せないようにして、誤解を招く案内が出る状態自体を防ぐ */}
          <button onClick={handleUpload} disabled={!file || isUploading || loadingCapacity}>
            アップロードして圧縮
          </button>
          <ProgressPanel stage={stage} errorMessage={errorMessage} />
        </div>

        {originalVideoUrl && (
          <div className="card">
            <h2>元動画 ({formatSize(originalFileSize)})</h2>
            {/* Chrome の既定 preload="auto" だと選択直後に動画全体の読み込みが走るため、
                先頭のメタデータのみ読み込む "metadata" にする。 */}
            <video src={originalVideoUrl} controls preload="metadata" width="100%"></video>
          </div>
        )}

        {compressedVideoUrl && (
          <div className="card">
            <h3>圧縮完了</h3>
            <h2>圧縮後動画 ({formatSize(compressedFileSize)})</h2>
            {/* 圧縮前が不明なときは NaN / Infinity を出さず "-" にフォールバックする */}
            <p className="size-summary">
              圧縮前 {originalFileSize > 0 ? formatSize(originalFileSize) : "-"}
              {" → "}
              圧縮後 {compressedFileSize > 0 ? formatSize(compressedFileSize) : "-"}
              {(() => {
                // 圧縮後サイズが未取得(0)のときは削減率も「-」にする。
                // ここで compressedFileSize を見ないと、「圧縮後 -」と表示しながら
                // 削減率だけ「（100.0% 削減）」になり、表示が矛盾する。
                const rate = compressedFileSize > 0
                  ? reductionRate(originalFileSize, compressedFileSize)
                  : null;
                if (rate === null) return <span className="rate-unknown">（削減率 -）</span>;
                if (rate < 0) return <span className="rate-increase">{Math.abs(rate).toFixed(1)}% 増加</span>;
                return <span className="rate-decrease">（{rate.toFixed(1)}% 削減）</span>;
              })()}
            </p>
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
              <button onClick={createShareLink} disabled={isCreatingShare || isLocalhost()}>
                {isCreatingShare ? "共有リンク作成中..." : isLocalhost() ? "ローカル環境では利用不可" : "共有リンクを作成"}
              </button>
              {isLocalhost() && (
                <p className="localhost-notice">
                  ローカルホスト環境では共有機能は利用できません。本番環境でご利用ください。
                </p>
              )}
            </div>
            
            {shareUrl && (
              <div className="share-result">
                <h4>共有URL:</h4>
                {/* <input> は仕様上テキストを折り返せず、長いURLの全文表示と両立しないため、
                    折り返し可能なテキストブロックにする。コピーは copyShareUrl() が
                    state の shareUrl を navigator.clipboard.writeText するので、
                    表示要素を変えても全文がコピーされる点は変わらない。 */}
                <div className="share-url-container">
                  <p className="share-url-text">{shareUrl}</p>
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
          border: 1px solid var(--panel-border);
          border-radius: 8px;
          font-family: sans-serif;
          background-color: var(--bg);
          color: var(--text);
        }
        h1 {
          text-align: center;
          margin-bottom: 40px;
          color: var(--text);
        }
        .error {
            color: var(--ng);
            text-align: center;
        }
        .card {
          border: 1px solid var(--panel-border);
          padding: 25px;
          margin-bottom: 25px;
          border-radius: 8px;
          text-align: left;
          background-color: var(--panel);
          box-shadow: var(--shadow);
        }
        .control {
          margin-bottom: 20px;
        }
        .control label {
          display: block;
          margin-bottom: 10px;
          font-weight: bold;
          color: var(--muted);
          font-size: 1rem;
        }
        .hint {
          font-size: 0.9em;
          color: var(--muted);
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
          border: 1px solid var(--input-border);
          border-radius: 4px;
          font-size: 1rem;
          box-sizing: border-box;
          background-color: var(--input-bg);
          color: var(--text);
        }
        button {
          display: inline-block;
          padding: 12px 20px;
          background-color: var(--accent);
          color: var(--accent-contrast);
          border: none;
          border-radius: 4px;
          cursor: pointer;
          margin-top: 20px;
          font-size: 1.1rem;
          transition: background-color 0.3s ease;
          width: 100%;
        }
        button:hover {
          background-color: var(--accent-hover);
        }
        button:disabled {
          background-color: var(--panel-border);
          cursor: not-allowed;
        }
        .progress-bar-container {
          width: 100%;
          height: 20px;
          background-color: var(--panel-alt);
          border-radius: 10px;
          margin-top: 15px;
          overflow: hidden;
        }
        .progress-bar {
          height: 100%;
          background-color: var(--ok);
          text-align: center;
          line-height: 20px;
          color: var(--accent-contrast);
          transition: width 0.5s ease;
        }
        video {
            display: block;
            margin-top: 10px;
        }
        .size-summary {
          margin: 4px 0 12px;
          font-size: 0.95rem;
          color: var(--text);
        }
        /* 削減時は状態色を文字色にしない。ライトテーマの --ok は白背景に対し 3.13:1 で
           WCAG AA(4.5:1) を満たさないため（実測）。数値は --text で出す。 */
        .rate-decrease { font-weight: 600; }
        .rate-unknown  { color: var(--muted); }
        /* 増加時のみ注意色。--warn は両テーマとも明るい琥珀色なので、
           必ず背景として使い、文字は専用トークン --warn-contrast を載せる（docs\APP_STANDARD.md §9-5）。
           実測コントラスト ライト 10.2:1 / ダーク 11.0:1。 */
        .rate-increase {
          display: inline-block;
          margin-left: 6px;
          padding: 1px 8px;
          border-radius: 10px;
          background-color: var(--warn);
          color: var(--warn-contrast);
          font-size: 0.85rem;
          font-weight: 600;
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
          color: var(--muted);
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
          background-color: var(--panel-alt);
          border-radius: 8px;
          border: 1px solid var(--panel-border);
        }

        .share-result h4 {
          margin-top: 0;
          margin-bottom: 10px;
          color: var(--text);
        }

        .share-url-container {
          display: flex;
          flex-wrap: wrap;          /* 収まらない幅では自動で縦積みになる */
          align-items: flex-start;
          gap: 10px;
          margin-bottom: 10px;
        }

        .share-url-text {
          flex: 1 1 240px;          /* 240px を下回るならボタンが次行へ折り返す（縦積み） */
          min-width: 0;             /* 必須。flex アイテムの既定 min-width:auto が縮小を妨げる */
          margin: 0;
          padding: 8px;
          border: 1px solid var(--input-border);
          border-radius: 4px;
          background-color: var(--panel-alt);
          color: var(--text);
          font-family: monospace;
          font-size: 0.9rem;
          line-height: 1.5;
          overflow-wrap: anywhere;  /* 区切りの無い長いURLでも折り返して全文を見せる */
          word-break: break-all;    /* 旧ブラウザ向けフォールバック */
          user-select: all;         /* クリック1回で全文を選択できる */
        }

        .copy-button {
          /* 上位の button { width: 100%; margin-top: 20px } を明示的に打ち消す。
             打ち消さないと flex-basis が 100% になり、隣の URL 欄が潰れて数文字しか見えなくなる
             （これが今回のレイアウト崩れの原因だった）。 */
          width: auto;
          margin-top: 0;
          flex: 0 0 auto;           /* 内容幅に固定し、URL 欄を潰さない */
          align-self: flex-start;
          padding: 8px 16px;
          background-color: var(--inconclusive);
          color: var(--accent-contrast);
          border: none;
          border-radius: 4px;
          cursor: pointer;
          font-size: 0.9rem;
          white-space: nowrap;
        }

        .copy-button:hover {
          filter: brightness(0.88);
        }

        .share-note {
          font-size: 0.9rem;
          color: var(--muted);
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
          background-color: color-mix(in srgb, var(--ok) 15%, var(--panel));
          color: var(--ok);
          border: 1px solid color-mix(in srgb, var(--ok) 40%, transparent);
        }

        .share-message.error {
          background-color: color-mix(in srgb, var(--ng) 15%, var(--panel));
          color: var(--ng);
          border: 1px solid color-mix(in srgb, var(--ng) 40%, transparent);
        }

        .video-actions {
          display: flex;
          flex-direction: column;
          gap: 10px;
          margin-top: 15px;
        }

        .download-note {
          font-size: 0.9rem;
          color: var(--accent);
          margin: 0;
          text-align: center;
          font-weight: 500;
        }

        .localhost-notice {
          font-size: 0.9em;
          color: var(--ng);
          margin-top: 10px;
          text-align: center;
        }


      `}</style>
    </>
  );
}
