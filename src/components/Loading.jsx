import { LoaderCircle } from 'lucide-react';
import Brand from './Brand';

export default function Loading() { return <main className="loading"><Brand /><LoaderCircle className="spin" /><p>SETTING THE FIELD</p></main>; }
