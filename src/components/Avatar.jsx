import { avatarPattern } from '../lib/avatar';

// Renders a player's current avatar: a small deterministic pixel/identicon
// pattern if they have one (see lib/avatar.js), or the old initial-letter
// swatch as a graceful fallback for legacy/unauthenticated rows with no
// linked PlayerProfile. Never derives anything from `name` when a designId
// is present — the pattern depends only on the ID.
export default function Avatar({ designId, name = '', size = 34 }) {
  if (designId == null) {
    return <span className="avatar" style={{ width: size, height: size, fontSize: size * 0.4 }}>{(name || '?')[0]}</span>;
  }
  const { color, cells } = avatarPattern(designId);
  const inner = Math.round(size * 0.72);
  return (
    <span className="avatar avatar--pixel" style={{ width: size, height: size }}>
      <svg width={inner} height={inner} viewBox="0 0 5 5" shapeRendering="crispEdges">
        {cells.map(([x, y]) => <rect key={`${x}-${y}`} x={x} y={y} width={1} height={1} fill={color} />)}
      </svg>
    </span>
  );
}
