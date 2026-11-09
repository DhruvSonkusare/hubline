import { NextResponse } from "next/server";

import { auth } from "@/auth";

// Route guard (Next 16 "proxy", formerly middleware): everything except the login page and
// the Auth.js endpoints needs a session.
export default auth((request) => {
  const { pathname, search } = request.nextUrl;
  if (request.auth || pathname === "/login" || pathname.startsWith("/api/auth")) {
    return NextResponse.next();
  }
  const login = new URL("/login", request.nextUrl);
  if (pathname !== "/") login.searchParams.set("next", pathname + search);
  return NextResponse.redirect(login);
});

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\.(?:png|svg|ico)$).*)"],
};
