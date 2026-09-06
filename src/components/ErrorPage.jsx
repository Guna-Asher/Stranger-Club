import { ChevronRight } from 'lucide-react';
import Brand from './Brand';

export default function ErrorPage({ message }) { return <main className="error-page"><Brand /><h1>WE CAN’T FIND<br />THIS MATCH.</h1><p>{message}</p><a className="primary-button" href="/">BACK TO STRANGER CLUB <ChevronRight size={19} /></a></main>; }
