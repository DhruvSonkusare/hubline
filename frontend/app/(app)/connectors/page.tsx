import { ConnectorCard } from "@/components/ConnectorCard";
import { ConnectResultToast } from "@/components/ConnectResultToast";
import { WakingScreen } from "@/components/WakingScreen";
import { api, BackendUnavailable, type Connector } from "@/lib/backend";

export const metadata = { title: "Connectors · Hubline" };

type Search = {
  connected?: string | string[];
  error?: string | string[];
  connector?: string | string[];
};

const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);

export default async function ConnectorsPage({ searchParams }: { searchParams: Promise<Search> }) {
  const sp = await searchParams;
  let connectors: Connector[];
  try {
    connectors = await api.connectors();
  } catch (error) {
    if (error instanceof BackendUnavailable) return <WakingScreen />;
    throw error;
  }
  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">Connectors</h1>
        <p className="text-sm text-slate-500">
          Each app is a plug-in. Connect it, then switch it on. Teams, LinkedIn and X arrive in
          Step 6.
        </p>
      </div>
      <ConnectResultToast
        connected={one(sp.connected)}
        error={one(sp.error)}
        connector={one(sp.connector)}
      />
      <div className="grid gap-4 md:grid-cols-2">
        {connectors.map((c) => (
          <ConnectorCard key={c.key} connector={c} />
        ))}
      </div>
    </div>
  );
}
