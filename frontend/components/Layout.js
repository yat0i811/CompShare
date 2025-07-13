import React, { useState, useEffect } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/router';
import useAuth from '../hooks/useAuth';

const Layout = ({ children }) => {
    const [sidebarOpen, setSidebarOpen] = useState(false);
    const router = useRouter();
    const { isAdmin, handleLogout } = useAuth();

    const toggleSidebar = () => {
        setSidebarOpen(!sidebarOpen);
    };

    const onLogout = () => {
        handleLogout();
        router.push('/');
    };

    return (
        <div className="layout-container">
            <header className="header">
                <button className="hamburger-button" onClick={toggleSidebar}>
                    ☰
                </button>
                <Link href="/">
                    <button className="logo-button">CompShare</button>
                </Link>
            </header>

            <div className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
                <button className="close-button" onClick={toggleSidebar}>×</button>
                <nav>
                    <ul>
                        <li>
                            <Link href="/">
                                <button>ホーム</button>
                            </Link>
                        </li>
                        <li>
                            <Link href="/manage">
                                <button>動画管理ページ</button>
                            </Link>
                        </li>
                        {isAdmin && (
                            <li>
                                <Link href="/admin">
                                    <button>管理者ページ</button>
                                </Link>
                            </li>
                        )}
                        <li className="logout-button-container">
                            <button onClick={onLogout}>ログアウト</button>
                        </li>
                    </ul>
                </nav>
            </div>

            <main className="main-content">
                {children}
            </main>

            {sidebarOpen && <div className="overlay" onClick={toggleSidebar}></div>}

            <style jsx>{`
                .layout-container {
                    display: flex;
                    flex-direction: column;
                    min-height: 100vh;
                }
                .header {
                    width: 100%;
                    background-color: #f0f0f0;
                    padding: 10px;
                    display: flex;
                    align-items: center;
                }
                .hamburger-button {
                    font-size: 24px;
                    margin-right: 10px;
                    background: none;
                    border: none;
                    cursor: pointer;
                }
                .logo-button {
                    cursor: pointer;
                    font-weight: bold;
                    color: #333;
                    background: none;
                    border: none;
                    font-size: 1.2em;
                    padding: 0;
                    margin: 0;
                }
                .logo-button:hover {
                    color: #999;
                }
                .sidebar {
                    position: fixed;
                    top: 0;
                    left: -250px;
                    width: 250px;
                    height: 100%;
                    background-color: #fff;
                    box-shadow: 2px 0 5px rgba(0,0,0,0.5);
                    transition: left 0.3s ease;
                    z-index: 1000;
                    padding-top: 50px;
                }
                .sidebar.open {
                    left: 0;
                }
                .close-button {
                    position: absolute;
                    top: 10px;
                    right: 10px;
                    font-size: 24px;
                    background: none;
                    border: none;
                    cursor: pointer;
                }
                .sidebar nav ul {
                    list-style: none;
                    padding: 0;
                    margin: 0;
                }
                .sidebar nav li {
                    padding: 10px;
                    border-bottom: 1px solid #eee;
                }
                .sidebar nav li button {
                    width: 100%;
                    text-align: left;
                    background: none;
                    border: none;
                    cursor: pointer;
                    font-size: 1em;
                    color: #333;
                    padding: 8px 12px;
                    border-radius: 4px;
                    transition: background-color 0.2s ease;
                }
                .sidebar nav li button:hover {
                    background-color: #f5f5f5;
                }
                .sidebar nav li.logout-button-container {
                    margin-top: auto;
                    border-top: 1px solid #eee;
                    border-bottom: none;
                    padding: 15px 10px;
                }
                .sidebar nav li.logout-button-container button {
                    width: 100%;
                    text-align: left;
                    background: none;
                    border: none;
                    cursor: pointer;
                    font-size: 1em;
                    color: #333;
                    padding: 8px 12px;
                    border-radius: 4px;
                    transition: background-color 0.2s ease;
                }
                .sidebar nav li.logout-button-container button:hover {
                    background-color: #f5f5f5;
                }
                .sidebar nav a {
                    text-decoration: none;
                    color: #333;
                    display: block;
                }
                .main-content {
                    flex-grow: 1;
                    padding: 20px;
                    margin-left: auto;
                    margin-right: auto;
                    width: 100%;
                    max-width: 800px;
                    margin-top: 50px;
                }
                .overlay {
                    position: fixed;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    background-color: rgba(0,0,0,0.5);
                    z-index: 999;
                }
            `}</style>
        </div>
    );
};

export default Layout; 