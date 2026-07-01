import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider, Navigate } from "react-router-dom";
import ConversationList from "./pages/ConversationList";
import Conversation from "./pages/Conversation";
import "./styles.css";

const router = createBrowserRouter(
  [
    { path: "/", element: <ConversationList /> },
    { path: "/c/:platform/:id", element: <Conversation /> },
    { path: "*", element: <Navigate to="/" replace /> },
  ],
  { basename: "/m" },
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
