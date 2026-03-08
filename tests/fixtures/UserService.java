package com.example.auth;

import com.example.repo.UserRepository;
import com.example.model.User;
import org.springframework.stereotype.Service;
import org.springframework.web.bind.annotation.*;
import java.util.Optional;

/**
 * Service layer for user management operations.
 * Handles CRUD operations and business logic for {@link User} entities.
 */
@Service
public class UserService implements IUserService {

    private final UserRepository userRepo;

    public UserService(UserRepository userRepo) {
        this.userRepo = userRepo;
    }

    /**
     * Retrieves a user by their unique ID.
     *
     * @param id The unique identifier of the user
     * @return The matching {@link User} object
     * @throws UserNotFoundException if no user found with given ID
     */
    public User getUser(Long id) {
        return userRepo.findById(id)
                .orElseThrow(() -> new UserNotFoundException(id));
    }

    /**
     * Creates a new user in the system.
     *
     * @param user The user entity to persist
     * @return The saved user with generated ID
     */
    public User createUser(User user) {
        return userRepo.save(user);
    }

    /**
     * Deletes a user by their unique ID.
     *
     * @param id The unique identifier of the user to delete
     * @deprecated Use {@link #deleteUserByEmail(String)} for audit compliance
     */
    @Deprecated
    public void deleteUser(Long id) {
        userRepo.delete(id);
    }

    /**
     * Deletes a user by their email address for audit compliance.
     *
     * @param email The email address of the user to delete
     * @see UserRepository#findByEmail
     */
    public void deleteUserByEmail(String email) {
        User user = userRepo.findByEmail(email)
                .orElseThrow(() -> new UserNotFoundException(email));
        userRepo.delete(user.getId());
    }
}
