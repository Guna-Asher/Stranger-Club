import { BrowserRouter, Route, Routes, useParams } from 'react-router-dom';
import AdminLogin from './admin/AdminLogin';
import AdminApp from './admin/AdminApp';
import Landing from './player/Landing';
import PlayerApp from './player/PlayerApp';
import Profile from './player/Profile';
import './index.css';

function PlayerRoute() {
  const { publicId } = useParams();
  return <PlayerApp publicId={publicId} />;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/admin/login" element={<AdminLogin />} />
        <Route path="/admin/*" element={<AdminApp />} />
        <Route path="/profile" element={<Profile />} />
        <Route path="/m/:publicId/*" element={<PlayerRoute />} />
        <Route path="/events/:publicId/*" element={<PlayerRoute />} />
        <Route path="/" element={<Landing />} />
        <Route path="*" element={<Landing />} />
      </Routes>
    </BrowserRouter>
  );
}
