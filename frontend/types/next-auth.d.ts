import "next-auth";

declare module "next-auth" {
  interface Session {
    /** Identity sent to the backend as X-Actor ("password:owner" or "github:<login>"). */
    actor?: string;
  }
}
