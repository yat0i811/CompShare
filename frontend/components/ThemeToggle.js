import { useEffect, useState } from 'react';

/* ============================================================
   テーマ切替ボタン（ライト既定 / ダーク切替）
   docs\APP_STANDARD.md §9 を参照。

   - localStorage のキー "theme"（全アプリ共通）に選択を保存する
   - OS の設定（prefers-color-scheme）は見ない。既定は常にライト
   - 絵文字・表示テキストは使わない。線画の SVG（月／太陽）のみ
   - aria-label / title / aria-pressed は読み上げ・ホバー説明用に必ず付ける
   ============================================================ */

const STORAGE_KEY = 'theme';
const LIGHT = 'light';
const DARK = 'dark';

function readInitialTheme() {
    if (typeof document === 'undefined') return LIGHT;
    // <head> のインライン script（_document.js）が既に data-theme を確定させているため、
    // マウント時はそれをそのまま読み取る（FOUC 防止と二重管理を避けるため）。
    return document.documentElement.getAttribute('data-theme') === DARK ? DARK : LIGHT;
}

const ThemeToggle = () => {
    const [theme, setTheme] = useState(LIGHT);

    useEffect(() => {
        setTheme(readInitialTheme());
    }, []);

    useEffect(() => {
        document.documentElement.setAttribute('data-theme', theme);
    }, [theme]);

    const handleToggle = () => {
        const next = theme === DARK ? LIGHT : DARK;
        setTheme(next);
        try {
            window.localStorage.setItem(STORAGE_KEY, next);
        } catch (e) {
            // プライベートモード等で保存できなくても、このセッション内では動作を継続する
        }
    };

    const label = theme === DARK ? 'ライトモードに切り替える' : 'ダークモードに切り替える';

    return (
        <button
            type="button"
            className="theme-toggle"
            onClick={handleToggle}
            aria-label={label}
            title={label}
            aria-pressed={theme === DARK}
        >
            {theme === DARK ? (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
                    <circle cx="12" cy="12" r="4.2" />
                    <path d="M12 2.5v2.2M12 19.3v2.2M4.2 4.2l1.6 1.6M18.2 18.2l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.2 19.8l1.6-1.6M18.2 5.8l1.6-1.6" />
                </svg>
            ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
                    <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
                </svg>
            )}
        </button>
    );
};

export default ThemeToggle;
