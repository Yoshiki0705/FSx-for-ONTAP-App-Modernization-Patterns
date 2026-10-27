package com.example.docintake;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * Candidate B entry point: a Spring Boot application with no web server. The first slice is a
 * background worker that processes an NFS-mounted inbox using a Java file lock for exclusive claim.
 * Web, HTTP, UI, AWS SDK, S3 Access Points, ECS, EKS, and container paths are out of this slice.
 */
@SpringBootApplication
public class DocIntakeBApplication {

    public static void main(String[] args) {
        // WebApplicationType defaults to NONE because no web starter is on the classpath.
        SpringApplication.run(DocIntakeBApplication.class, args);
    }
}
