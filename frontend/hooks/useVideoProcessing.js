import { useState, useEffect, useRef } from "react";
import { v4 as uuidv4 } from "uuid";
import {
    BASE_URL, 
    GET_UPLOAD_URL_ENDPOINT, 
    COMPRESS_URL_ENDPOINT, 
    WS_URL_BASE,
    isLocalhost,
    isTokenExpired
} from '../utils/constants';

// Custom hook for video processing logic
export default function useVideoProcessing({ token, handleLogout }) {
  const [file, setFile] = useState(null);
  const [originalVideoUrl, setOriginalVideoUrl] = useState("");
  const [originalFileSize, setOriginalFileSize] = useState(0);
  const [compressedVideoUrl, setCompressedVideoUrl] = useState("");
  const [compressedFileName, setCompressedFileName] = useState("");
  const [compressedFileSize, setCompressedFileSize] = useState(0);
  const [progress, setProgress] = useState(0);
  const [clientId] = useState(uuidv4());
  const [crf, setCrf] = useState(28);
  const [resolution, setResolution] = useState("source");
  const [customWidth, setCustomWidth] = useState("");
  const [customHeight, setCustomHeight] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [modifiedFile, setModifiedFile] = useState(null);
  const [modifiedVideoUrl, setModifiedVideoUrl] = useState("");

  const ws = useRef(null);

  const isExternal = typeof window !== "undefined" && !isLocalhost();
  const MAX_FILE_SIZE = 1000 * 1024 * 1024;

  useEffect(() => {
    if (!token || !clientId) return;

    if (ws.current && ws.current.readyState === WebSocket.OPEN) {
      ws.current.close();
    }
    
    const socketUrl = `${WS_URL_BASE}/${clientId}?token=${token}`;
    ws.current = new WebSocket(socketUrl);

    ws.current.onopen = () => {
    };
    
    ws.current.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === "done") {
          setCompressedVideoUrl(data.url);
          setCompressedFileName(data.filename);
          setCompressedFileSize(data.size || 0);
          setProgress(100);
        } else if (data.type === "progress") {
          setProgress(data.value);
        } else if (data.type === "error") {
          setErrorMessage(data.detail || "サーバーで圧縮エラーが発生しました。");
          setIsUploading(false);
        }
      } catch (err) {
        console.warn("WebSocket JSON parse error:", e.data, err);
      }
    };

    ws.current.onerror = (error) => {
      console.error("WebSocket error:", error);
    };

    ws.current.onclose = (event) => {
    };

    return () => {
      if (ws.current) {
        ws.current.close();
      }
    };
  }, [clientId, token]);

  const formatSize = (bytes) => `${(bytes / 1024 / 1024).toFixed(2)} MB`;

  const estimateCompressedSize = (originalSize, crfValue) => {
    const baseCrf = 18;
    const compressionRate = 0.1285;
    const factor = Math.pow(1 - compressionRate, crfValue - baseCrf);
    return originalSize * factor;
  };

  const handleUpload = async () => {
    if (!file || !token || isUploading) return;

    if (isTokenExpired(token)) {
      alert("セッションが切れました。再ログインしてください。");
      handleLogout();
      return;
    }

    setIsUploading(true);
    setErrorMessage("");
    setProgress(0);
    setCompressedVideoUrl("");
    setCompressedFileSize(0);
    setOriginalVideoUrl(URL.createObjectURL(file));
    setOriginalFileSize(file.size);

    try {
      if (!file.type.startsWith("video/")) {
        setErrorMessage("動画ファイルのみアップロードできます（例: mp4）。サポートされている形式か確認してください。");
        setIsUploading(false);
        return;
      }
      
      if (isExternal && file.size > MAX_FILE_SIZE) {
        setErrorMessage("外部アクセスでは1GBを超える動画はアップロードできません。");
        setIsUploading(false);
        return;
      }

      const getUrlRes = await fetch(`${GET_UPLOAD_URL_ENDPOINT}?filename=${encodeURIComponent(file.name)}&file_size=${file.size}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!getUrlRes.ok) {
        const errorData = await getUrlRes.json().catch(() => ({ detail: "サーバーでのアップロードURL取得に失敗しました。" }));
        setErrorMessage(errorData.detail || "アップロードURL取得に失敗しました。ファイル形式やサイズを確認してください。");
        setIsUploading(false);
        return;
      }
      const data = await getUrlRes.json();
      const { upload_url, key } = data;
      if (!upload_url || !key) {
        setErrorMessage("署名付きアップロードURLまたはキーが無効です。");
        setIsUploading(false);
        return;
      }

      const r2UploadRes = await fetch(upload_url, { method: "PUT", body: file });
      if (!r2UploadRes.ok) {
        setErrorMessage("R2へのファイルアップロードに失敗しました。");
        setIsUploading(false);
        return;
      }
      
      const compressFormData = new FormData();
      compressFormData.append("filename", file.name);
      compressFormData.append("crf", crf);
      compressFormData.append("resolution", resolution);
      if (resolution === "custom") {
        if (!customWidth || !customHeight || parseInt(customWidth, 10) <= 0 || parseInt(customHeight, 10) <= 0) {
          setErrorMessage("カスタム解像度の幅と高さには正の数値を入力してください。");
          setIsUploading(false);
          return;
        }
        compressFormData.append("width", customWidth);
        compressFormData.append("height", customHeight);
      }
      compressFormData.append("client_id", clientId);
      compressFormData.append("key", key);

      const compressRes = await fetch(COMPRESS_URL_ENDPOINT, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: compressFormData,
      });
      
      if (!compressRes.ok) {
        const errorData = await compressRes.json().catch(() => ({ detail: "R2経由での圧縮処理の開始に失敗しました。" }));
        setErrorMessage(errorData.detail || "圧縮処理の開始に失敗しました。サーバーログを確認してください。");
        setIsUploading(false);
        return;
      }
      
    } catch (err) {
      console.error("アップロード中にエラーが発生しました:", err);
      setErrorMessage("アップロード中にエラーが発生しました。ファイル形式やサイズを確認してください。");
      setIsUploading(false);
    }
  };

  const downloadCompressedVideo = () => {
    if (!compressedVideoUrl) return;

    // Fetch the video data
    fetch(compressedVideoUrl)
      .then(response => response.blob())
      .then(blob => {
        // Create a blob URL
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = blobUrl;
        a.download = compressedFileName || "compressed_video.mp4";
        document.body.appendChild(a);
        a.click();
        // Clean up by revoking the blob URL and removing the link
        document.body.removeChild(a);
        URL.revokeObjectURL(blobUrl);
      })
      .catch(error => {
        console.error("Error downloading the video:", error);
        setErrorMessage("動画のダウンロード中にエラーが発生しました。");
      });
  };

  return {
    file, setFile,
    originalVideoUrl, setOriginalVideoUrl,
    originalFileSize, setOriginalFileSize,
    compressedVideoUrl, setCompressedVideoUrl,
    compressedFileName, setCompressedFileName,
    compressedFileSize, setCompressedFileSize,
    progress, setProgress,
    crf, setCrf,
    resolution, setResolution,
    customWidth, setCustomWidth,
    customHeight, setCustomHeight,
    isUploading,
    errorMessage, setErrorMessage,
    modifiedFile, setModifiedFile,
    modifiedVideoUrl, setModifiedVideoUrl,
    handleUpload,
    downloadCompressedVideo,
    formatSize,
    estimateCompressedSize,
    MAX_FILE_SIZE,
  };
}