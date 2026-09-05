import { BrowserRouter, Route, Routes, useParams } from 'react-router-dom';
import AdminLogin from './admin/AdminLogin';
import AdminApp from './admin/AdminApp';
import PlayerApp from './player/PlayerApp';
import { DEFAULT_MATCH } from './lib/constants';
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
        <Route path="/m/:publicId/*" element={<PlayerRoute />} />
        <Route path="/events/:publicId/*" element={<PlayerRoute />} />
        <Route path="*" element={<PlayerApp publicId={DEFAULT_MATCH} />} />
      </Routes>
    </BrowserRouter>
  );
}
