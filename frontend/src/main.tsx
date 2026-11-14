import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import Shell from "./Shell";
import Dashboard from "./pages/Dashboard";
import Buyer from "./pages/Buyer";
import Approvals from "./pages/Approvals";
import Orders from "./pages/Orders";
import OrderDetail from "./pages/OrderDetail";
import Audit from "./pages/Audit";
import Policy from "./pages/Policy";
import Agents from "./pages/Agents";
import Trust from "./pages/Trust";
import ProductDetail from "./pages/ProductDetail";
import { MOCK } from "./api";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {MOCK ? (
      // Loud on purpose: fixture data must never be mistaken for the live gateway in a demo.
      // In the page flow, not fixed: a fixed badge always ends up covering something at some width.
      <div className="border-b border-warn/30 bg-warn-soft py-1 text-center text-[11px] font-semibold text-warn">
        MOCK DATA — fixtures, not the live gateway
      </div>
    ) : null}
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/buyer" element={<Buyer />} />
          <Route path="/approvals" element={<Approvals />} />
          <Route path="/orders" element={<Orders />} />
          <Route path="/orders/:id" element={<OrderDetail />} />
          <Route path="/products/:id" element={<ProductDetail />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/policy" element={<Policy />} />
          <Route path="/agents" element={<Agents />} />
          <Route path="/trust" element={<Trust />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
