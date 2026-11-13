// Bitunix Futures — Java reference signing (MessageDigest, no third-party deps).
// Compile & run: javac BitunixSign.java && java BitunixSign
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

public class BitunixSign {

    public static String sha256hex(String s) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] d = md.digest(s.getBytes(StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder();
        for (byte b : d) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }

    /** sorted key+value concatenation, no '=' and no '&', empty skipped. */
    public static String signQueryString(Map<String, String> params) {
        if (params == null) return "";
        List<String> keys = new ArrayList<>(params.keySet());
        Collections.sort(keys);
        StringBuilder sb = new StringBuilder();
        for (String k : keys) {
            String v = params.get(k);
            if (v == null || v.isEmpty()) continue;
            sb.append(k).append(v);
        }
        return sb.toString();
    }

    /** sign = sha256(sha256(nonce + timestamp + apiKey + query + body) + secret). */
    public static String sign(String apiKey, String secret, String nonce,
                              String timestamp, String query, String body)
            throws Exception {
        String message = nonce + timestamp + apiKey + query + body;
        String digest = sha256hex(message);
        return sha256hex(digest + secret);
    }

    public static void main(String[] args) throws Exception {
        final String apiKey = "11111111111111111";
        final String secret = "22222222222222222";
        final String nonce = "33333333333333333";
        final String timestamp = "1747104711262";
        final String want = "40e87256554e263e39201a3979469b7fbb640d1c7c41759abf5c6a18f937f922";

        String got = sign(apiKey, secret, nonce, timestamp, "marginCoinUSDT", "");
        if (!got.equals(want)) {
            System.out.println("FAIL sign vector: " + got);
            System.exit(1);
        }
        String wrong = sign(apiKey, secret, nonce, timestamp, "marginCoin=USDT", "");
        if (wrong.equals(want)) {
            System.out.println("FAIL urlencoded-guard");
            System.exit(1);
        }
        System.out.println("PASS sign vector / urlencoded-guard");
    }
}