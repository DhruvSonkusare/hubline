import { LoginForm } from "@/components/LoginForm";

export const metadata = { title: "Log in · Hubline" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const next = typeof sp.next === "string" ? sp.next : "/inbox";
  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="card w-full max-w-sm p-8">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl bg-accent text-lg font-bold text-white">
            H
          </div>
          <h1 className="text-2xl font-semibold">Hubline</h1>
          <p className="mt-1 text-sm text-slate-500">One inbox. You approve every reply.</p>
        </div>
        <LoginForm next={next} />
      </div>
    </main>
  );
}
