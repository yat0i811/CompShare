import '../styles/globals.css';
import dynamic from 'next/dynamic';

const DynamicLayout = dynamic(() => import('../components/Layout'), { ssr: false });

function MyApp({ Component, pageProps }) {
  return (
    <DynamicLayout>
      <Component {...pageProps} />
    </DynamicLayout>
  );
}

export default MyApp; 