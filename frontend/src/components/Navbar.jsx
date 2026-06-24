import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { Film, Link2, CheckCircle2, LogOut } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { getConfig, API } from "@/lib/api";
import axios from "axios";

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
  const [cfg, setCfg] = useState({ adobe_connected: false, adobe_user: null });

  const load = () => getConfig().then(setCfg).catch(() => {});

  useEffect(() => {
    load();
    // detect ?adobe_connected=1 from OAuth callback
    const params = new URLSearchParams(window.location.search);
    if (params.get("adobe_connected") === "1") {
      toast.success("Frame.io connected.");
      window.history.replaceState({}, "", window.location.pathname);
      load();
    }
    if (params.get("adobe_error")) {
      toast.error("Frame.io connect failed: " + params.get("adobe_error"));
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  const onConnect = () => {
    window.location.href = `${API}/auth/adobe/login`;
  };

  const onDisconnect = async () => {
    await axios.post(`${API}/auth/adobe/logout`);
    toast.success("Disconnected");
    load();
  };

  return (
    <nav
      data-testid="navbar"
      className="sticky top-0 z-50 backdrop-blur-xl bg-black/60 border-b border-white/10"
    >
      <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
        <Link to="/" data-testid="navbar-logo" className="flex items-center gap-2 group">
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
          {cfg.adobe_connected ? (
            <Button
              onClick={onDisconnect}
              variant="ghost"
              data-testid="disconnect-frameio-btn"
              className="text-xs uppercase tracking-widest font-mono-tech text-emerald-500 hover:text-rose-500 hover:bg-transparent rounded-none"
            >
              <CheckCircle2 className="w-4 h-4 mr-1" /> Connected
              <LogOut className="w-3 h-3 ml-2 opacity-60" />
            </Button>
          ) : (
            <Button
              onClick={onConnect}
              data-testid="connect-frameio-btn"
              className="bg-rose-600 hover:bg-rose-500 text-white border-0 h-9 px-4 font-mono-tech uppercase tracking-widest text-[10px] rounded-none"
            >
              <Link2 className="w-3 h-3 mr-2" /> Connect Frame.io
            </Button>
          )}
        </div>
      </div>
    </nav>
  );
}
