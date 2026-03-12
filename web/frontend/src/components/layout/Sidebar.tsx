import { NavLink, useLocation } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import { useUIStore } from '../../stores/uiStore';
import { useEffect } from 'react';

const navItems = [
  { path: '/', label: 'Dashboard', icon: '◉' },
  { path: '/bots', label: 'Bots', icon: '⚙' },
  { path: '/strategies', label: 'Strategies', icon: '◎' },
  { path: '/portfolio', label: 'Portfolio', icon: '◈' },
  { path: '/backtesting', label: 'Backtesting', icon: '▶' },
  { path: '/events', label: 'Events', icon: '◇' },
  { path: '/settings', label: 'Settings', icon: '☰' },
];

export function Sidebar() {
  const { sidebarOpen, closeSidebar } = useUIStore();
  const location = useLocation();

  // Close sidebar on route change (mobile)
  useEffect(() => {
    closeSidebar();
  }, [location.pathname, closeSidebar]);

  const sidebarContent = (
    <>
      <div className="p-5 border-b border-border">
        <h1 className="text-xl font-bold text-text font-[Manrope]">
          <span className="text-primary">TRADER</span>AGENT
        </h1>
        <p className="text-xs text-text-muted mt-1">v2.0 Dashboard</p>
      </div>

      <nav className="flex-1 py-4">
        {navItems.map((item) => (
          <NavLink
            key={item.path}
            to={item.path}
            className={({ isActive }) =>
              `flex items-center gap-3 px-5 py-3 text-sm transition-colors ${
                isActive
                  ? 'bg-primary/20 text-primary border-r-2 border-primary'
                  : 'text-text-muted hover:bg-surface-hover hover:text-text'
              }`
            }
          >
            <span className="text-lg">{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
      </nav>

      <div className="p-4 border-t border-border">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-profit animate-pulse" />
          <span className="text-xs text-text-muted">System Online</span>
        </div>
      </div>
    </>
  );

  return (
    <>
      {/* Desktop sidebar */}
      <aside className="hidden md:flex fixed left-0 top-0 h-full w-60 bg-surface border-r border-border flex-col z-10">
        {sidebarContent}
      </aside>

      {/* Mobile overlay */}
      <AnimatePresence>
        {sidebarOpen && (
          <>
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="fixed inset-0 bg-black/60 z-40 md:hidden"
              onClick={closeSidebar}
            />
            <motion.aside
              initial={{ x: -240 }}
              animate={{ x: 0 }}
              exit={{ x: -240 }}
              transition={{ duration: 0.2, ease: 'easeOut' }}
              className="fixed left-0 top-0 h-full w-60 bg-surface border-r border-border flex flex-col z-50 md:hidden"
            >
              {sidebarContent}
            </motion.aside>
          </>
        )}
      </AnimatePresence>
    </>
  );
}
