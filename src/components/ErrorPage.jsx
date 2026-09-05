import { ChevronRight } from 'lucide-react';
import Brand from './Brand';
import { DEFAULT_MATCH } from '../lib/constants';

export default function ErrorPage({ message }) { return <main className="error-page"><Brand /><h1>WE CAN’T FIND<br />THIS MATCH.</h1><p>{message}</p><a className="primary-button" href={`/m/${DEFAULT_MATCH}`}>VIEW LATEST MATCH <ChevronRight size={19} /></a></main>; }
