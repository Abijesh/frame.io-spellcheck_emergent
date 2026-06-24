import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

export const api = axios.create({ baseURL: API });

export const createAnalysis = async ({ frameioUrl, transcript, autoPost, videoFile }) => {
  const form = new FormData();
  if (frameioUrl) form.append("frameio_url", frameioUrl);
  if (transcript) form.append("transcript", transcript);
  form.append("auto_post", autoPost ? "true" : "false");
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

export const deleteAnalysis = async (id) => {
  const { data } = await api.delete(`/analyses/${id}`);
  return data;
};

export const getConfig = async () => {
  const { data } = await api.get("/config");
  return data;
};
