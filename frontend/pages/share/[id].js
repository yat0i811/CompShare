import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/router';
import Link from 'next/link';
import { BASE_URL } from '../../utils/constants';

// ページ内ローカルのサイズ整形。undefined/0以下のときは "NaN undefined" ではなく "-" を返す。
const formatFileSize = (bytes) => {
    if (typeof bytes !== 'number' || bytes <= 0) return '-';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
};

const SharePage = () => {
    const router = useRouter();
    const { id } = router.query;

    // 画面の排他状態。従来は isLoading + fileExists の2値管理だったが、
    // それでは「期限切れ(410)」と「そもそも存在しない(404)」を区別できなかった。
    const [status, setStatus] = useState('loading'); // loading | ready | notfound | expired | error
    const [info, setInfo] = useState(null);
    const [errorDetail, setErrorDetail] = useState('');

    useEffect(() => {
        // router.isReady を見ずに id を使うと、初回レンダリングでは id が undefined のまま
        // fetch してしまい、常に404扱いになる（Next.js のクエリ確定タイミングの問題）。
        if (!router.isReady || !id) return;

        let aborted = false;

        (async () => {
            try {
                // なぜ Next.js の API ルートを経由せずブラウザから直接 /be を叩くのか:
                // 以前はサーバー側（Next.js API ルート）がバックエンドの応答を
                // response.arrayBuffer() 等で一度メモリに丸ごと載せてから中継していたため、
                // 大きな動画では Node.js のヒープに動画全体が乗って OOM でプロセスがクラッシュしていた。
                // メタ情報の取得だけでも同じ経路上に Node を置きたくないため、
                // ブラウザ（クライアントサイド JS）から nginx 経由でバックエンドへ直接 fetch する。
                const res = await fetch(`${BASE_URL}/share/${encodeURIComponent(id)}/info`);
                if (aborted) return;

                if (res.ok) {
                    setInfo(await res.json());
                    setStatus('ready');
                } else if (res.status === 404) {
                    setStatus('notfound');
                } else if (res.status === 410) {
                    setStatus('expired');
                } else {
                    setStatus('error');
                    setErrorDetail(`サーバーエラー (HTTP ${res.status})`);
                }
            } catch (e) {
                if (!aborted) {
                    setStatus('error');
                    setErrorDetail('ネットワークエラーが発生しました。');
                }
            }
        })();

        return () => { aborted = true; };
    }, [router.isReady, id]);

    // BASE_URL はモジュール評価時に window の有無で値が変わる（ローカルは絶対URL、
    // 本番は '/be' の相対パス）ため、SSR 描画時と CSR 描画後で文字列が食い違い、
    // Next.js のハイドレーション不一致を起こす。status の初期値は 'loading' なので、
    // これらの URL を使う JSX は status === 'ready'（＝クライアントで確定済み）のときだけ描画する。
    const previewUrl = status === 'ready' ? `${BASE_URL}/share/${encodeURIComponent(id)}/preview` : '';
    const downloadUrl = status === 'ready' ? `${BASE_URL}/share/${encodeURIComponent(id)}/download` : '';

    if (status === 'loading') {
        return (
            <div className="error-container">
                <div className="loading-spinner"></div>
                <p>ファイル情報を取得しています...</p>
            </div>
        );
    }

    if (status === 'notfound') {
        return (
            <div className="error-container">
                <h1>ファイルが見つかりません</h1>
                <p className="error-message">共有リンクが無効か、ファイルが削除されています。</p>
                <div className="action-buttons">
                    <Link href="/">
                        <button className="home-button">ホームに戻る</button>
                    </Link>
                </div>
            </div>
        );
    }

    if (status === 'expired') {
        return (
            <div className="error-container">
                <h1>有効期限が切れています</h1>
                <p className="error-message">この共有リンクは有効期限を過ぎたため、アクセスできません。</p>
                <div className="action-buttons">
                    <Link href="/">
                        <button className="home-button">ホームに戻る</button>
                    </Link>
                </div>
            </div>
        );
    }

    if (status === 'error') {
        return (
            <div className="error-container">
                <h1>情報を取得できませんでした</h1>
                <p className="error-message">{errorDetail}</p>
                <div className="action-buttons">
                    <Link href="/">
                        <button className="home-button">ホームに戻る</button>
                    </Link>
                </div>
            </div>
        );
    }

    // status === 'ready'
    const isVideo = typeof info.content_type === 'string' && info.content_type.startsWith('video/');

    return (
        <div className="share-container">
            <div className="file-info">
                <h1>ファイルの共有</h1>

                {isVideo && (
                    <video
                        className="share-video"
                        controls
                        // preload="auto" にしない: ページを開いただけで動画全体の転送が始まり、
                        // R2 の帯域（Class B 相当の転送）を無駄に消費するため。
                        // ユーザーが実際に再生を押すまでは先頭のメタデータだけで十分。
                        preload="metadata"
                        playsInline
                        src={previewUrl}
                        // <source type={...}> は付けない。type が実体の Content-Type とずれると
                        // ブラウザが再生自体を拒否することがあるため、レスポンスヘッダの
                        // Content-Type にブラウザの型判定を委ねる。
                    />
                )}

                <div className="file-details">
                    <p><strong>ファイル名:</strong> {info.filename}</p>
                    <p><strong>サイズ:</strong> {formatFileSize(info.size)}</p>
                    <p><strong>有効期限:</strong> {new Date(info.expiry_date).toLocaleString('ja-JP')}</p>
                    <p><strong>残り日数:</strong> {info.remaining_days === 0 ? '残り 1 日未満' : `残り ${info.remaining_days} 日`}</p>
                </div>

                {/*
                  ダウンロードはブラウザネイティブの <a href> 直リンクにする。
                  JS の fetch / blob / createObjectURL は使わない（それが元の OOM の根本原因だった。
                  fetch → response.blob() でファイル全体をブラウザのメモリに載せてから
                  createObjectURL で疑似的な a.click() を行っていたのは Next.js API ルート側の
                  Node.js だけでなく、この経路でも大容量ファイルではブラウザタブ自体を不安定にする）。
                  download 属性も付けない: バックエンドが
                  Content-Disposition: attachment; filename*=UTF-8''... を返しており、
                  正しい日本語ファイル名は既にそちらで指定済みのため。
                */}
                <a className="download-button" href={downloadUrl}>ダウンロード</a>

                <p className="share-notice">
                    このリンクは有効期限を過ぎるとアクセスできなくなります。
                </p>
            </div>
        </div>
    );
};

export default SharePage;
