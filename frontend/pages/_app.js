import "../styles/globals.css";
import dynamic from "next/dynamic";
import { AuthProvider } from "../context/AuthContext";

const DynamicLayout = dynamic(() => import("../components/Layout"), { ssr: false });

function MyApp({ Component, pageProps }) {
  return (
    <AuthProvider>
      <DynamicLayout>
        <Component {...pageProps} />
      </DynamicLayout>
    </AuthProvider>
  );
}

export default MyApp;
