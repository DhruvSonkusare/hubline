import NextAuth, { type NextAuthConfig } from "next-auth";
import Credentials from "next-auth/providers/credentials";
import GitHub from "next-auth/providers/github";

import { actorFor, enabledProviders, isAllowedGithubLogin } from "@/lib/auth-config";
import {
  AttemptLimiter,
  configuredPassword,
  FAILED_LOGIN_DELAY_MS,
  sleep,
  verifyPassword,
} from "@/lib/password";

const providerIds = enabledProviders();
const limiter = new AttemptLimiter();

const providers: NextAuthConfig["providers"] = [];

if (providerIds.includes("password")) {
  const expected = configuredPassword(); // fail fast: refuse to start with a weak/missing password
  providers.push(
    Credentials({
      id: "password",
      name: "Password",
      credentials: { password: { label: "Password", type: "password" } },
      async authorize(credentials, request) {
        const ip =
          request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
          request.headers.get("x-real-ip") ||
          "local";
        if (limiter.isBlocked(ip)) {
          await sleep(FAILED_LOGIN_DELAY_MS);
          return null;
        }
        const input = typeof credentials?.password === "string" ? credentials.password : "";
        if (!verifyPassword(input, expected)) {
          limiter.recordFailure(ip);
          await sleep(FAILED_LOGIN_DELAY_MS);
          return null;
        }
        limiter.reset(ip);
        return { id: "owner", name: "Owner" };
      },
    }),
  );
}

if (providerIds.includes("github")) {
  providers.push(GitHub); // reads AUTH_GITHUB_ID / AUTH_GITHUB_SECRET
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers,
  logger: {
    // A wrong password is expected traffic, not a server error: keep logs for real problems.
    error(error) {
      const kind = (error as { type?: string }).type ?? error.name;
      if (kind === "CredentialsSignin") return;
      console.error("[auth]", error);
    },
  },
  session: { strategy: "jwt", maxAge: 7 * 24 * 60 * 60 },
  pages: { signIn: "/login" },
  callbacks: {
    signIn({ account, profile }) {
      if (account?.provider === "github") return isAllowedGithubLogin(profile?.login);
      return true;
    },
    jwt({ token, account, profile }) {
      if (account) token.actor = actorFor(account.provider, profile?.login);
      return token;
    },
    session({ session, token }) {
      return { ...session, actor: typeof token.actor === "string" ? token.actor : "password:owner" };
    },
  },
});
