import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { translations } from '../i18n/translations';

const PreferencesContext = createContext();

export function PreferencesProvider({ children }) {
  // Safe local storage fallback
  const getStorage = (key, defaultVal) => {
    try {
      return localStorage.getItem(key) || defaultVal;
    } catch (e) {
      return defaultVal;
    }
  };
  
  const setStorage = (key, val) => {
    try {
      localStorage.setItem(key, val);
    } catch (e) {}
  };

  const [lang, setLangState] = useState(() => {
    const val = getStorage('app-language', 'vi');
    return ['vi', 'en'].includes(val) ? val : 'vi';
  });
  const [theme, setThemeState] = useState(() => {
    const val = getStorage('app-theme', 'system');
    return ['light', 'dark', 'system'].includes(val) ? val : 'system';
  });
  const [computedTheme, setComputedTheme] = useState('light');

  const setLang = useCallback((newLang) => {
    setLangState(newLang);
    setStorage('app-language', newLang);
    document.documentElement.lang = newLang;
  }, []);

  const applyTheme = useCallback((themeValue) => {
    let actualTheme = themeValue;
    if (themeValue === 'system') {
      actualTheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    setComputedTheme(actualTheme);
    document.documentElement.setAttribute('data-theme', actualTheme);
    document.documentElement.style.colorScheme = actualTheme;
  }, []);

  const setTheme = useCallback((newTheme) => {
    setThemeState(newTheme);
    setStorage('app-theme', newTheme);
    applyTheme(newTheme);
  }, [applyTheme]);

  useEffect(() => {
    document.documentElement.lang = lang;
    document.title = translations[lang]?.brand || translations['vi'].brand || 'Trạm Dịch';
    applyTheme(theme);

    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => {
      if (theme === 'system') {
        applyTheme('system');
      }
    };
    
    // Modern browsers
    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener('change', handler);
      return () => mediaQuery.removeEventListener('change', handler);
    } 
    // Fallback
    else if (mediaQuery.addListener) {
      mediaQuery.addListener(handler);
      return () => mediaQuery.removeListener(handler);
    }
  }, [theme, lang, applyTheme]);

  const t = useCallback((key, params = {}) => {
    const dict = translations[lang] || translations['vi'];
    let text = dict[key] || translations['vi'][key] || key;
    
    // Interpolate params
    Object.keys(params).forEach(p => {
      text = text.replace(new RegExp(`\\{${p}\\}`, 'g'), params[p]);
    });
    
    return text;
  }, [lang]);

  return (
    <PreferencesContext.Provider value={{ lang, setLang, theme, setTheme, computedTheme, t }}>
      {children}
    </PreferencesContext.Provider>
  );
}

export function usePreferences() {
  return useContext(PreferencesContext);
}
