import { Link, useLocation } from "react-router-dom";
import { Film } from "lucide-react";

const NavLink = ({ to, children, testid }) => {
  const { pathname } = useLocation();
  const active = pathname === to;
  return (
    <Link
      to={to}
      data-testid={testid}
      className={`text-sm tracking-wide transition-colors ${
        active ? "text-white" : "text-zinc-500 hover:text-white"
      }`}
    >
      {children}
    </Link>
  );
};

export default function Navbar() {
  return (
    <nav
      data-testid="navbar"
      className="sticky top-0 z-50 backdrop-blur-xl bg-black/60 border-b border-white/10"
    >
      <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
        <Link
          to="/"
          data-testid="navbar-logo"
          className="flex items-center gap-2 group"
        >
          <div className="w-8 h-8 border border-rose-600 flex items-center justify-center group-hover:bg-rose-600 transition-colors duration-200">
            <Film className="w-4 h-4 text-rose-600 group-hover:text-white" strokeWidth={2} />
          </div>
          <span className="font-display font-black text-lg tracking-tighter">
            PROOF<span className="text-rose-600">.IO</span>
          </span>
        </Link>
        <div className="flex items-center gap-8">
          <NavLink to="/" testid="nav-home">New analysis</NavLink>
          <NavLink to="/history" testid="nav-history">History</NavLink>
          <a
            href="https://developer.frame.io"
            target="_blank"
            rel="noreferrer"
            className="text-sm text-zinc-500 hover:text-white transition-colors"
            data-testid="nav-docs"
          >
            Docs
          </a>
        </div>
      </div>
    </nav>
  );
}
