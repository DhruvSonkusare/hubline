import { ConnectorCard } from "@/components/ConnectorCard";
import { WakingScreen } from "@/components/WakingScreen";
import { api, BackendUnavailable, type Connector } from "@/lib/backend";

export const metadata = { title: "Connectors · Hubline" };

export default async function ConnectorsPage() {
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
          Each app is a plug-in. Slack and Gmail arrive in Step 5; Teams, LinkedIn and X in Step 6.
        </p>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        {connectors.map((c) => (
          <ConnectorCard key={c.key} connector={c} />
        ))}
      </div>
    </div>
  );
}
