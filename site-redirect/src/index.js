// Old and alternate hostnames for the decomp.wtf website: send every request to https://decomp.wtf, same path.
// The agent network's API (api.decomp.online) is a separate hostname and is not affected.
export default {
  fetch(request) {
    const url = new URL(request.url);
    url.protocol = "https:";
    url.hostname = "decomp.wtf";
    url.port = "";
    return Response.redirect(url.toString(), 301);
  },
};
