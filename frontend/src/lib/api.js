import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

// withCredentials: the Frame.io OAuth session lives in an httpOnly cookie set
// by the backend (different port = different origin from the frontend), so
// it has to be explicitly opted into on every request.
export const api = axios.create({ baseURL: API, withCredentials: true });

export const createAnalysis = async ({ frameioUrl, transcript, password, autoPost, checkContrast, videoFile }) => {
  const form = new FormData();
  if (frameioUrl) form.append("frameio_url", frameioUrl);
  if (transcript) form.append("transcript", transcript);
  if (password) form.append("password", password);
  form.append("auto_post", autoPost ? "true" : "false");
  form.append("check_contrast", checkContrast ? "true" : "false");
  if (videoFile) form.append("video", videoFile);
  const { data } = await api.post("/analyses", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
};

export const getAnalysis = async (id) => {
  const { data } = await api.get(`/analyses/${id}`);
  return data;
};

export const listAnalyses = async () => {
  const { data } = await api.get("/analyses");
  return data;
};

export const postComments = async (id) => {
  const { data } = await api.post(`/analyses/${id}/post`);
  return data;
};

export const postSingleIssue = async (analysisId, issueId) => {
  const { data } = await api.post(
    `/analyses/${analysisId}/issues/${issueId}/post`
  );
  return data;
};

export const deleteAnalysis = async (id) => {
  const { data } = await api.delete(`/analyses/${id}`);
  return data;
};

export const getConfig = async () => {
  const { data } = await api.get("/config");
  return data;
};

export const getFrameioStatus = async () => {
  const { data } = await api.get("/frameio/oauth/status");
  return data;
};

// Real browser navigation, not a fetch -- has to go through Adobe's login UI.
export const connectFrameioUrl = `${API}/frameio/oauth/authorize`;

export const disconnectFrameio = async () => {
  const { data } = await api.post("/frameio/oauth/disconnect");
  return data;
};
