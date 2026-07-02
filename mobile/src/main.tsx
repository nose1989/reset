import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider, Navigate } from "react-router-dom";
import ConversationList from "./pages/ConversationList";
import Conversation from "./pages/Conversation";
import { primeSound, startNewMessageWatcher } from "./sound";
import "./styles.css";

// Unlock audio on the first user gesture so new-message beeps can play later.
const unlock = () => {
  primeSound();
  window.removeEventListener("pointerdown", unlock);
  window.removeEventListener("keydown", unlock);
};
window.addEventListener("pointerdown", unlock);
window.addEventListener("keydown", unlock);

// Beep ("di di") when new messages arrive, regardless of which page is open.
startNewMessageWatcher();

const router = createBrowserRouter([
  { path: "/", element: <ConversationList /> },
  { path: "/c/:cid", element: <Conversation /> },
  { path: "*", element: <Navigate to="/" replace /> },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
