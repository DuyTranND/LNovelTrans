import React, { useState, useEffect } from 'react';
import App from './App';
import * as api from './api';
import { usePreferences } from './contexts/PreferencesContext';
import { AlertCircle, X, Sun, Moon, Loader2 } from 'lucide-react';

export default function AuthGate() {
  const { t, lang, setLang, computedTheme, setTheme } = usePreferences();
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [logoutError, setLogoutError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isLoggingOut, setIsLoggingOut] = useState(false);

  useEffect(() => {
    let active = true;
    api.getAuthMe()
      .then((data) => {
        if (active) {
          setUser(data);
          setLoading(false);
        }
      })
      .catch(() => {
        if (active) {
          setUser(null);
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  const handleLogin = async (e) => {
    e.preventDefault();
    setError('');
    setIsSubmitting(true);
    try {
      const data = await api.login(username, password);
      setUser(data);
      setPassword('');
      setLogoutError('');
    } catch (err) {
      setError(err.message || t('errLoginFailed'));
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleLogout = async () => {
    setIsLoggingOut(true);
    setLogoutError('');
    try {
      await api.logout();
      setUser(null);
    } catch (err) {
      setLogoutError(err.message || t('errLogoutFailed'));
    } finally {
      setIsLoggingOut(false);
    }
  };

  const getRoleLabel = (role) => {
    if (!role) return '';
    if (role === 'admin') return t('roleAdmin');
    if (role === 'editor') return t('roleEditor');
    if (role === 'viewer') return t('roleViewer');
    return role;
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh', fontFamily: 'sans-serif', color: '#555' }}>
        <Loader2 size={24} className="animate-spin" style={{ marginRight: 8 }} />
        <span>Loading...</span>
      </div>
    );
  }

  if (!user) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', minHeight: '100vh', backgroundColor: 'var(--color-rail-bg)', fontFamily: 'sans-serif', position: 'relative' }}>
        {/* Top bar controls */}
        <div style={{ position: 'absolute', top: 16, right: 16, display: 'flex', gap: 8 }}>
          <button
            type="button"
            className="btn btn-icon-sm"
            onClick={() => setTheme(computedTheme === 'dark' ? 'light' : 'dark')}
            title={t('themeToggle')}
            aria-label={t('themeToggle')}
            style={{ border: 'none', background: 'transparent' }}
          >
            {computedTheme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}
          </button>
          <button
            type="button"
            className="btn btn-icon-sm"
            onClick={() => setLang(lang === 'vi' ? 'en' : 'vi')}
            title={t('langToggle')}
            aria-label={t('langToggle')}
            style={{ border: 'none', background: 'transparent', fontWeight: 600, fontSize: 13 }}
          >
            {lang.toUpperCase()}
          </button>
        </div>

        <form onSubmit={handleLogin} style={{ width: '320px', padding: '24px', backgroundColor: 'var(--color-bg)', borderRadius: 'var(--radius)', boxShadow: '0 4px 12px rgba(0, 0, 0, 0.1)', border: '1px solid var(--color-border)', display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <h2 style={{ margin: 0, textAlign: 'center', fontSize: '1.25rem', color: 'var(--color-text)' }}>
            {t('loginTitle')}
          </h2>

          {error && (
            <div
              className="alert-banner alert-banner-error"
              style={{ padding: '8px 12px', fontSize: '0.875rem' }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <AlertCircle size={16} />
                <span>{error}</span>
              </div>
            </div>
          )}

          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            <label style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--color-text)' }}>
              {t('username')}
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              disabled={isSubmitting}
              className="form-control"
              style={{ padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: 'var(--radius)', fontSize: '0.875rem' }}
            />
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            <label style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--color-text)' }}>
              {t('password')}
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              disabled={isSubmitting}
              className="form-control"
              style={{ padding: '8px 12px', border: '1px solid var(--color-border)', borderRadius: 'var(--radius)', fontSize: '0.875rem' }}
            />
          </div>

          <button
            type="submit"
            disabled={isSubmitting}
            className="btn btn-primary"
            style={{ padding: '8px 16px', fontWeight: 600, fontSize: '0.875rem', marginTop: '8px', justifyContent: 'center' }}
          >
            {isSubmitting ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                <span>{t('signingIn')}</span>
              </>
            ) : (
              <span>{t('signIn')}</span>
            )}
          </button>
        </form>
      </div>
    );
  }

  return (
    <div>
      {/* Top Auth Banner */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: '12px', padding: '6px 16px', backgroundColor: '#1f2937', color: '#f9fafb', fontSize: '0.875rem', borderBottom: '1px solid #374151' }}>
        <span>
          {t('loggedInAs')}{' '}
          <strong>{user.username || 'User'}</strong>
          {user.role && (
            <span style={{ marginLeft: 6, padding: '1px 6px', fontSize: 11, borderRadius: 4, backgroundColor: user.role === 'admin' ? '#7c3aed' : user.role === 'editor' ? '#2563eb' : '#4b5563', color: '#ffffff' }}>
              {getRoleLabel(user.role)}
            </span>
          )}
        </span>
        <button
          type="button"
          onClick={handleLogout}
          disabled={isLoggingOut}
          style={{ padding: '4px 10px', backgroundColor: '#374151', color: '#f9fafb', border: '1px solid #4b5563', borderRadius: '4px', cursor: isLoggingOut ? 'not-allowed' : 'pointer', fontSize: '0.75rem', display: 'flex', alignItems: 'center', gap: 4 }}
        >
          {isLoggingOut && <Loader2 size={12} className="animate-spin" />}
          <span>{isLoggingOut ? t('loggingOut') : t('logout')}</span>
        </button>
      </div>

      {/* Visible Logout Failure Banner */}
      {logoutError && (
        <div
          className="alert-banner alert-banner-error"
          style={{ margin: '8px 16px 0', zIndex: 100 }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <AlertCircle size={16} />
            <span>{logoutError}</span>
          </div>
          <button
            type="button"
            className="btn btn-icon-sm"
            onClick={() => setLogoutError('')}
            aria-label={t('ariaCloseError')}
            style={{ border: 'none', background: 'transparent', color: 'inherit' }}
          >
            <X size={16} />
          </button>
        </div>
      )}

      <App user={user} />
    </div>
  );
}
