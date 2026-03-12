import { createBrowserRouter } from 'react-router-dom';
import { AppLayout } from '../components/layout/AppLayout';
import { ProtectedRoute } from './ProtectedRoute';
import { Login } from '../pages/Login';
import { Dashboard } from '../pages/Dashboard';
import { Bots } from '../pages/Bots';
import { BotDetail } from '../pages/BotDetail';
import { Strategies } from '../pages/Strategies';
import { Portfolio } from '../pages/Portfolio';
import { Backtesting } from '../pages/Backtesting';
import { Settings } from '../pages/Settings';
import { EventOntology } from '../pages/EventOntology';

export const router = createBrowserRouter([
  {
    path: '/login',
    element: <Login />,
  },
  {
    element: <ProtectedRoute />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { path: '/', element: <Dashboard /> },
          { path: '/bots', element: <Bots /> },
          { path: '/bots/:botName', element: <BotDetail /> },
          { path: '/strategies', element: <Strategies /> },
          { path: '/portfolio', element: <Portfolio /> },
          { path: '/backtesting', element: <Backtesting /> },
          { path: '/events', element: <EventOntology /> },
          { path: '/settings', element: <Settings /> },
        ],
      },
    ],
  },
]);
